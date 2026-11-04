/**
 * oss-scout Issue Hunter — Cloudflare Worker（KV 拉取模式，无需隧道/回传端点）。
 *
 * 每 1 分钟：GitHub REST 增量轮询 → 预筛 → 发现写入 KV（pending:* 键，TTL 1h）
 * 本地 dashboard 每分钟用 CLOUDFLARE_API_TOKEN 拉取 KV → 入队 → 删除已处理键。
 *
 * KV 免费档仅 1000 write/天。写预算：diag ≤48/天 + since 水位 48/天 + 发现批次（偶发）
 * = 常况 ~100/天，留足余量。中间每分钟重查同一窗口由本地 seen.json 去重。
 *
 * Secrets（npx wrangler secret put）：GH_TOKEN
 * vars（wrangler.toml）：REPOS
 * 绑定：KV namespace SEEN（wrangler.toml 里填 id）
 */

const EXCLUDED_LABELS = ["invalid", "wontfix", "duplicate", "question", "stale",
  "awaiting more info", "awaiting-more-info", "needs more info", "needs-repro",
  "documentation"];

// 补扫只认最近的 issue：太老的可修概率急剧下降（早被别人认领或修复），
// 而且补扫每轮都跑，不设龄上限会把仓库里所有历史 issue 反复送进评分。
const BACKFILL_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000;

function collectIssue(issues, repo, it) {
  if (it.pull_request || (it.assignees && it.assignees.length)) return;
  const labels = (it.labels || []).map(l => (typeof l === "string" ? l : l.name));
  if (labels.some(l => EXCLUDED_LABELS.includes(l.toLowerCase()))) return;
  issues.push({
    repo, number: it.number, title: it.title, url: it.html_url,
    created_at: it.created_at, updated_at: it.updated_at,
    labels, reactions: (it.reactions && it.reactions.total_count) || 0,
    comments: it.comments || 0,
    // 8000：AI 评分要判断"这个 issue 能不能自动修"，依据是复现步骤、预期行为、
    // 环境说明。4000 常常在代码块中间截断，把可修的 issue 判成"信息不足"。
    body_excerpt: (it.body || "").slice(0, 8000),
  });
}

// 补扫专用：拿到的是"最近创建"列表而不是增量，龄上限在这里卡。
function collectBackfillIssue(issues, repo, it, now) {
  const age = now.getTime() - new Date(it.created_at).getTime();
  if (age > BACKFILL_MAX_AGE_MS || age < 0) return;
  collectIssue(issues, repo, it);
}

function ghHeaders(token) {
  return {
    "Authorization": `Bearer ${token}`,
    "Accept": "application/vnd.github+json",
    "User-Agent": "oss-scout-worker",
  };
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(pollOnce(env));
  },
};

async function pollOnce(env) {
  const repos = (env.REPOS || "").split(",").map(s => s.trim()).filter(Boolean);
  if (!repos.length) return;
  const now = new Date();

  // 水位：优先 KV；每 5 分钟落盘一次（省 KV 写额度），60s 重叠 + 本地去重防漏防重
  let since = await env.SEEN.get("since");
  if (!since) {
    since = new Date(now.getTime() - 15 * 60 * 1000).toISOString();
  } else {
    since = new Date(new Date(since).getTime() - 60 * 1000).toISOString();
  }

  const issues = [];
  let ok_repos = 0;
  const errors = [];
  for (const repo of repos) {
    try {
      const r = await fetch(
        `https://api.github.com/repos/${repo}/issues?since=${since}&state=open&per_page=50`,
        { headers: ghHeaders(env.GH_TOKEN) });
      if (!r.ok) {
        errors.push(`${repo}: HTTP ${r.status}`);
        continue;
      }
      ok_repos++;
      const list = await r.json();
      for (const it of list) {
        collectIssue(issues, repo, it);
      }
    } catch (e) {
      errors.push(`${repo}: ${String(e).slice(0, 80)}`);
    }
  }

  // 补扫（round-robin）：`since` 水位只向前看，仓库新加入池子时，水位之前的
  // 既有 issue 永远不会被看到——除非它们后来又被更新。agentgateway#4256 就
  // 是这么漏掉的：issue 比"仓库进池子"早了半天，之后没人再更新它，等维护者
  // 指派/关闭时已经过了我们能接手的窗口。每分钟补扫一个仓库（50 仓一轮
  // ≈50 分钟），每天多花 ~1440 次 API 调用（限额 5000/h，无压力），任何
  // 仓库进池后半小时内就有完整的最近-issue 视野。
  if (repos.length) {
    const rr = repos[now.getUTCMinutes() % repos.length];
    try {
      const r = await fetch(
        `https://api.github.com/repos/${rr}/issues?state=open&sort=created` +
        `&direction=desc&per_page=20`,
        { headers: ghHeaders(env.GH_TOKEN) });
      if (r.ok) {
        for (const it of await r.json()) {
          collectBackfillIssue(issues, rr, it, now);
        }
      } else {
        errors.push(`${rr} (backfill): HTTP ${r.status}`);
      }
    } catch (e) {
      errors.push(`${rr} (backfill): ${String(e).slice(0, 80)}`);
    }
  }

  // 诊断键：降频到每 30 分钟 或 有错误时写。
  // KV 免费档仅 1000 write/天——原来每分钟写 = 1440/天，单这一项就超额（2026-10-25 实测 2139）。
  const diagDue = now.getUTCMinutes() % 30 === 0 || errors.length > 0;
  if (diagDue) {
    await env.SEEN.put("diag", JSON.stringify({
      at: now.toISOString(), ok_repos, errors: errors.slice(0, 5),
      found: issues.length, repos: repos.length,
      has_token: Boolean(env.GH_TOKEN),
    }));
  }

  // 水位每 30 分钟推进（48 写/天）。中间每分钟重查同一窗口靠本地 seen.json 去重，
  // 不产生重复候选；GitHub core API 5000/h 足够（14 仓 × 60 = 840/h）。
  if (ok_repos > 0 && now.getUTCMinutes() % 30 === 0) {
    await env.SEEN.put("since", now.toISOString());
  }

  if (!issues.length) return;

  // 主通道：直接 POST 到 dashboard /api/ingest/issues（免 KV 写额度）。
  // 兜底：INGEST_URL 不可达时才写 KV pending（KV 免费档仅 1000 write/天）。
  const payload = JSON.stringify({
    repos_scanned: repos.length, scanned_at: now.toISOString(), issues,
  });
  let delivered = false;
  if (env.INGEST_URL && env.INGEST_TOKEN) {
    try {
      const resp = await fetch(env.INGEST_URL, {
        method: "POST",
        headers: {"Content-Type": "application/json",
                  "X-Ingest-Token": env.INGEST_TOKEN},
        body: payload,
      });
      delivered = resp.ok;
    } catch (e) {
      delivered = false;
    }
  }
  if (!delivered) {
    const key = `pending:${now.toISOString().replace(/[:.]/g, "-")}`;
    await env.SEEN.put(key, payload, {expirationTtl: 3600});
  }

  // PR 评论：我们开出去的 PR 有人说话就尽快告诉 VPS。
  // 这条和 issues 扫描分开做、失败不影响上面——PR 评论的时效性要求更高
  // （维护者提了意见，48 小时内必须响应），但漏一轮不会丢单。
  await notifyPrComments(env, now);
}

// 我们的 PR 列表存在：优先 KV 的 "prs" 键（VPS 定时写入），其次 wrangler.toml
// 的 PRS 静态变量。动态列表必须走 KV——它每小时都在变，写进 toml 就意味着每次
// 增删 PR 都要重新部署 Worker。
async function notifyPrComments(env, now) {
  let list = [];
  try {
    const stored = await env.SEEN.get("prs");
    if (stored) list = JSON.parse(stored);
  } catch (e) { /* 回落到静态变量 */ }
  if (!list.length) {
    list = (env.PRS || "").split(",").map(s => s.trim()).filter(Boolean);
  }
  if (!list.length || !env.INGEST_URL || !env.INGEST_TOKEN) return;

  let since = await env.SEEN.get("pr_since");
  if (!since) {
    since = new Date(now.getTime() - 30 * 60 * 1000).toISOString();
  } else {
    // 重叠 10 分钟：评论是单条事件，漏掉就真漏了，宁可重复（VPS 侧按 comment id 去重）
    since = new Date(new Date(since).getTime() - 10 * 60 * 1000).toISOString();
  }

  const events = [];
  for (const item of list) {
    const [repo, numRaw] = item.split("#");
    const num = parseInt(numRaw, 10);
    if (!repo || !num) continue;
    try {
      const r = await fetch(
        `https://api.github.com/repos/${repo}/issues/${num}/comments?since=${since}&per_page=20`,
        { headers: ghHeaders(env.GH_TOKEN) });
      if (!r.ok) continue;
      for (const c of await r.json()) {
        const login = (c.user && c.user.login) || "";
        if (login === env.MY_LOGIN) continue;      // 自己的回复不触发
        events.push({
          repo, number: num, comment_id: c.id, author: login,
          created_at: c.created_at, body: (c.body || "").slice(0, 4000),
        });
      }
    } catch (e) { /* 单个 PR 失败不影响其它 */ }
  }

  if (!events.length) return;
  try {
    await fetch(env.INGEST_URL.replace("/issues", "/pr-comments"), {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-Ingest-Token": env.INGEST_TOKEN},
      body: JSON.stringify({events}),
    });
    // 水位只在成功投递后推进，失败下一轮会重试同一窗口
    if (now.getUTCMinutes() % 10 === 0) {
      await env.SEEN.put("pr_since", now.toISOString());
    }
  } catch (e) { /* VPS 不可达，下轮重试 */ }
}
