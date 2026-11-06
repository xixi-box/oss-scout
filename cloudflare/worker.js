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

function collectIssue(issues, repo, it, comments) {
  if (it.pull_request || (it.assignees && it.assignees.length)) return;
  const labels = (it.labels || []).map(l => (typeof l === "string" ? l : l.name));
  if (labels.some(l => EXCLUDED_LABELS.includes(l.toLowerCase()))) return;
  issues.push({
    repo, number: it.number, title: it.title, url: it.html_url,
    created_at: it.created_at, updated_at: it.updated_at,
    labels, reactions: (it.reactions && it.reactions.total_count) || 0,
    comments: it.comments || 0,
    // 16000：AI 细筛需要完整复现步骤、环境说明和全部评论
    // 环境说明。4000 常常在代码块中间截断，把可修的 issue 判成"信息不足"。
    body_excerpt: (it.body || "").slice(0, 16000),
    // 细筛需要评论：认领信号（I'll fix this）、维护者反馈、"已有人修"都在评论里
    issue_comments: (comments || []).slice(0, 20),
  });
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
  // 评论拉取（细筛需要评论：认领信号、维护者反馈、"已有人修"都在评论里）。
  // 轮转：每分钟给一个仓库拉最新 issue 的评论，50 仓 ≈ 50 分钟轮完一圈；
  // API 成本 1 call/分钟（限额 5000/h，无压力）。
  if (repos.length) {
    const cr_repo = repos[now.getUTCMinutes() % repos.length];
    try {
      const cr = await fetch(
        `https://api.github.com/repos/${cr_repo}/issues?state=open&sort=updated` +
        `&direction=desc&per_page=10`,
        { headers: ghHeaders(env.GH_TOKEN) });
      if (cr.ok) {
        for (const it of await cr.json()) {
          if (it.pull_request || (it.comments || 0) <= 0) continue;
          try {
            const cres = await fetch(
              `https://api.github.com/repos/${cr_repo}/issues/${it.number}/comments?per_page=30`,
              { headers: ghHeaders(env.GH_TOKEN) });
            const cs = cres.ok ? await cres.json() : [];
            collectIssue(issues, cr_repo, it, cs.map(c => ({
              author: (c.user && c.user.login) || "?",
              created_at: c.created_at || "",
              body: (c.body || "").slice(0, 1500),
            })));
          } catch (e) { /* 评论拉取失败不影响主发现 */ }
        }
      }
    } catch (e) {
      errors.push(`${cr_repo} (comments): ${String(e).slice(0, 80)}`);
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
