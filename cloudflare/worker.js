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
        if (it.pull_request || (it.assignees && it.assignees.length)) continue;
        const labels = (it.labels || []).map(l => (typeof l === "string" ? l : l.name));
        if (labels.some(l => EXCLUDED_LABELS.includes(l.toLowerCase()))) continue;
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
    } catch (e) {
      errors.push(`${repo}: ${String(e).slice(0, 80)}`);
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
}
