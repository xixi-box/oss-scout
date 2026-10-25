/**
 * oss-scout Issue Hunter — Cloudflare Worker 版（用户裁定 2026-10-25：从 VPS 剥离）。
 *
 * 每 1 分钟（cron trigger）用 GitHub REST（非 search API，避开 30 次/分钟搜索限额）
 * 轮询监控仓库的 issue 更新，轻量预筛后回传本地 ingest 端点入队。
 *
 * 额度账（免费档）：Workers 10 万次请求/天 >> 1440 次/天（1 次/分钟）；
 * GitHub core API 5000 次/小时 >> 每分钟 ~20 仓库各 1 次列表调用。
 *
 * 部署：见 cloudflare/README.md。Secrets：GH_TOKEN / INGEST_URL / INGEST_TOKEN。
 * 绑定 KV namespace（变量名 SEEN）做 `since` 水位与去重。
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
    await pollOnce(env);
  },
};

async function pollOnce(env) {
  const repos = (env.REPOS || "").split(",").map(s => s.trim()).filter(Boolean);
  if (!repos.length) return;
  const issues = [];

  for (const repo of repos) {
    const sinceKey = `since:${repo}`;
    // 冷启动回看 15 分钟；之后增量（上次轮询时间 - 60s 重叠防漏）
    let since = await env.SEEN.get(sinceKey);
    if (!since) {
      since = new Date(Date.now() - 15 * 60 * 1000).toISOString();
    } else {
      since = new Date(new Date(since).getTime() - 60 * 1000).toISOString();
    }
    try {
      const r = await fetch(
        `https://api.github.com/repos/${repo}/issues?since=${since}&state=open&per_page=50`,
        { headers: ghHeaders(env.GH_TOKEN) });
      if (!r.ok) continue;   // 限流/网络失败：跳过该仓库，下轮重试（水位不推进）
      const list = await r.json();
      for (const it of list) {
        // REST 的 issues 列表里 PR 会带 pull_request 字段 → 排除；排除有 assignee 的
        if (it.pull_request || (it.assignees && it.assignees.length)) continue;
        const labels = (it.labels || []).map(l => (typeof l === "string" ? l : l.name));
        if (labels.some(l => EXCLUDED_LABELS.includes(l.toLowerCase()))) continue;
        issues.push({
          repo, number: it.number, title: it.title, url: it.html_url,
          created_at: it.created_at, updated_at: it.updated_at,
          labels, reactions: (it.reactions && it.reactions.total_count) || 0,
          comments: it.comments || 0,
          body_excerpt: (it.body || "").slice(0, 4000),
        });
      }
      await env.SEEN.put(sinceKey, new Date().toISOString());
    } catch (e) {
      continue;   // 网络异常：水位不推进
    }
  }

  if (!issues.length) return;
  try {
    await fetch(env.INGEST_URL, {
      method: "POST",
      headers: {"Content-Type": "application/json",
                "X-Ingest-Token": env.INGEST_TOKEN},
      body: JSON.stringify({repos_scanned: repos.length, issues}),
    });
  } catch (e) {
    // 回传失败：issue 会因 since 水位已推进而漏掉一次——可接受（下轮 updated 不变不会重发，
    // 但本地 seen 未记录，后续 issue 更新会再触发）
  }
}
