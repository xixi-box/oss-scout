/**
 * Scan all configured repositories every minute. Per-repository cursors share a
 * KV value, checkpointed every 30 minutes; dashboard deduplication handles replays.
 */
const EXCLUDED_LABELS = ["invalid", "wontfix", "duplicate", "question", "stale",
  "awaiting more info", "awaiting-more-info", "needs more info", "needs-repro",
  "documentation"];

function collectIssue(issues, repo, it, rejected) {
  if (it.pull_request || (it.assignees || []).some(a => a.login.toLowerCase() !== "xixi-box")) {
    rejected.push({ repo, number: it.number, reason: "not_open_unassigned_issue" });
    return;
  }
  const labels = (it.labels || []).map(l => typeof l === "string" ? l : l.name);
  if (labels.some(l => EXCLUDED_LABELS.includes(l.toLowerCase()))) {
    rejected.push({ repo, number: it.number, reason: "excluded_label" });
    return;
  }
  issues.push({
    repo, number: it.number, title: it.title, url: it.html_url,
    created_at: it.created_at, updated_at: it.updated_at, labels,
    reactions: (it.reactions && it.reactions.total_count) || 0,
    comments: it.comments || 0, body_excerpt: (it.body || "").slice(0, 16000),
  });
}

function ghHeaders(token) {
  return { Authorization: `Bearer ${token}`, Accept: "application/vnd.github+json",
    "User-Agent": "oss-scout-worker" };
}

let activePoll;
export default {
  scheduled(event, env, ctx) {
    // Different isolates may replay a window. Downstream deduplication and
    // transactional claims handle that; skip overlapping scans in this isolate.
    if (!activePoll) {
      activePoll = pollOnce(env).finally(() => { activePoll = undefined; });
    }
    ctx.waitUntil(activePoll);
  },
};

async function readMap(env, key) {
  const raw = await env.SEEN.get(key);
  return raw ? JSON.parse(raw) : {};
}

function overlap(value, now, minutes) {
  const ms = Date.parse(value || "");
  return new Date((Number.isFinite(ms) ? ms : now.getTime() - 15 * 60000)
    - minutes * 60000).toISOString();
}

async function request(url, options, budget) {
  if (budget.remaining <= 0) throw new Error("Worker subrequest budget exhausted");
  budget.remaining--;
  return fetch(url, { ...options, signal: AbortSignal.timeout(10000) });
}

async function ghPages(url, token, budget) {
  const items = [];
  while (url) {
    // Never forward GH_TOKEN to a foreign pagination origin.
    if (new URL(url).origin !== "https://api.github.com") {
      throw new Error("unexpected pagination origin");
    }
    const response = await request(url, { headers: ghHeaders(token) }, budget);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const page = await response.json();
    if (!Array.isArray(page)) throw new Error("expected GitHub array");
    items.push(...page);
    const next = (response.headers.get("Link") || "").match(/<([^>]+)>;\s*rel="next"/);
    url = next ? next[1] : "";
  }
  return items;
}

async function checkpoint(env, key, previous, completed, now) {
  // Avoid moving a cursor backwards if another scan completed in the meantime.
  const latest = await readMap(env, key);
  const merged = { ...previous, ...latest };
  for (const item of completed) {
    if (!merged[item] || Date.parse(merged[item]) < now.getTime()) {
      merged[item] = now.toISOString();
    }
  }
  await env.SEEN.put(key, JSON.stringify(merged));
}

async function deliverIssues(env, issues, repoCount, now, budget) {
  for (let offset = 0; offset < issues.length; offset += 100) {
    const payload = JSON.stringify({ repos_scanned: repoCount,
      scanned_at: now.toISOString(), issues: issues.slice(offset, offset + 100) });
    let delivered = false;
    if (env.INGEST_URL && env.INGEST_TOKEN) {
      try {
        const response = await request(env.INGEST_URL, { method: "POST",
          headers: { "Content-Type": "application/json", "X-Ingest-Token": env.INGEST_TOKEN },
          body: payload }, budget);
        delivered = response.ok;
      } catch (e) { /* Durable fallback below. */ }
    }
    if (!delivered) {
      // Keep until consumed: an outage longer than an hour must not erase issues.
      // KV quota errors propagate, preventing unsafe cursor advancement.
      const content = JSON.stringify(issues.slice(offset, offset + 100));
      const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(content));
      const hash = Array.from(new Uint8Array(digest), x => x.toString(16).padStart(2, "0")).join("");
      const key = `pending:${hash}`;
      // Failed POSTs replay the same scan window. Reuse its durable batch instead
      // of spending a new KV write and retaining a second copy every minute.
      if (!await env.SEEN.get(key)) await env.SEEN.put(key, payload);
    }
  }
}

async function issueBatch(env, repos, marks, now, budget) {
  const result = { issues: [], rejected: [], completed: [], errors: [] };
  let pending = repos.map(repo => ({ repo, cursor: null, issues: [], rejected: [] }));
  while (pending.length) {
    const fields = pending.map((item, index) => {
      const [owner, name] = item.repo.split("/");
      const since = overlap(marks[item.repo], now, 1);
      return `r${index}: repository(owner:${JSON.stringify(owner)},name:${JSON.stringify(name)}) {
        issues(first:100,after:${JSON.stringify(item.cursor)},states:[OPEN],
          filterBy:{since:${JSON.stringify(since)}},orderBy:{field:UPDATED_AT,direction:ASC}) {
          nodes { number title url body createdAt updatedAt author { login }
            assignees(first:10) { nodes { login } } labels(first:100) { nodes { name } }
            comments { totalCount } reactions { totalCount } }
          pageInfo { hasNextPage endCursor }
        }
      }`;
    }).join("\n");
    let data;
    try {
      const response = await request("https://api.github.com/graphql", {
        method: "POST", headers: { ...ghHeaders(env.GH_TOKEN), "Content-Type": "application/json" },
        body: JSON.stringify({ query: `{ ${fields} }` }),
      }, budget);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      data = await response.json();
      if (!data.data) throw new Error("GraphQL data unavailable");
    } catch (error) {
      result.errors.push(...pending.map(item => `${item.repo}: ${String(error).slice(0, 100)}`));
      break;
    }
    const next = [];
    pending.forEach((item, index) => {
      const alias = `r${index}`;
      const connection = data.data[alias] && data.data[alias].issues;
      if (!connection || (data.errors || []).some(error => !error.path || error.path[0] === alias)) {
        result.errors.push(`${item.repo}: incomplete GraphQL result`);
        return;
      }
      for (const node of connection.nodes) {
        collectIssue(item.issues, item.repo, { ...node, html_url: node.url,
          created_at: node.createdAt, updated_at: node.updatedAt,
          assignees: node.assignees.nodes, labels: node.labels.nodes,
          comments: node.comments.totalCount, reactions: { total_count: node.reactions.totalCount } }, item.rejected);
      }
      if (connection.pageInfo.hasNextPage) {
        if (!connection.pageInfo.endCursor || connection.pageInfo.endCursor === item.cursor) {
          result.errors.push(`${item.repo}: invalid pagination cursor`);
          return;
        }
        next.push({ ...item, cursor: connection.pageInfo.endCursor });
      } else {
        result.issues.push(...item.issues);
        result.rejected.push(...item.rejected);
        result.completed.push(item.repo);
      }
    });
    pending = next;
  }
  return result;
}

async function pollIssues(env, now, budget) {
  const repos = (env.REPOS || "").split(",").map(s => s.trim()).filter(Boolean);
  if (!repos.length) return;
  const marks = await readMap(env, "watermarks");
  // Migration only: do not advance the old shared cursor again.
  const legacy = await env.SEEN.get("since");
  const recent = new Date(now.getTime() - 15 * 60000).toISOString();
  const initial = Date.parse(legacy) > Date.parse(recent) ? legacy : recent;
  let bootstrap = false;
  for (const repo of repos) {
    if (!marks[repo]) {
      marks[repo] = initial;
      bootstrap = true;
    }
  }
  // Persist the initial lower bound even if a repository is unavailable for
  // longer than the bootstrap window. This is initialization, not advancement.
  if (bootstrap) await env.SEEN.put("watermarks", JSON.stringify(marks));
  const issues = [];
  const rejected = [];
  const completed = [];
  const errors = [];
  // Ten repositories per GraphQL call: 57 repositories normally cost six
  // requests, leaving room for PR comments within the free Worker request cap.
  for (let start = 0; start < repos.length; start += 10) {
    const batch = await issueBatch(env, repos.slice(start, start + 10), marks, now, budget);
    issues.push(...batch.issues);
    rejected.push(...batch.rejected);
    completed.push(...batch.completed);
    errors.push(...batch.errors);
  }
  const completedAt = new Date().toISOString();
  await deliverIssues(env, issues, repos.length, now, budget);
  // Telemetry does not hold back a discovery cursor or consume its delivery budget.
  if (rejected.length && env.INGEST_URL && env.INGEST_TOKEN && budget.remaining) {
    try {
      await request(env.INGEST_URL, { method: "POST",
        headers: { "Content-Type": "application/json", "X-Ingest-Token": env.INGEST_TOKEN },
        body: JSON.stringify({ issues: [], coarse_completed_at: completedAt, coarse_rejected: rejected })
      }, budget);
    } catch (e) { console.warn("Coarse screening activity unavailable"); }
  }
  if (now.getUTCMinutes() % 30 === 0) {
    await checkpoint(env, "watermarks", marks, completed, now);
    await env.SEEN.put("diag", JSON.stringify({ at: now.toISOString(),
      ok_repos: completed.length, errors: errors.slice(0, 10), found: issues.length,
      repos: repos.length, has_token: Boolean(env.GH_TOKEN) }));
  }
}

async function pollOnce(env) {
  const now = new Date();
  // PR delivery is independent of empty issue scans and discovery failures.
  // Reserve independent budgets so neither channel can starve the other; the
  // combined maximum is below the free Worker's 50 external subrequests.
  const results = await Promise.allSettled([pollIssues(env, now, { remaining: 18 }),
    notifyPrComments(env, now, { remaining: 30 })]);
  const failed = results.find(result => result.status === "rejected");
  if (failed) throw failed.reason;
}

async function notifyPrComments(env, now, budget = { remaining: 30 }) {
  const normalize = repo => repo.trim().toLowerCase().replace(/^jlowin\/fastmcp$/, "prefecthq/fastmcp");
  const roster = new Set((env.TRACKED_REPOS || env.REPOS || "").split(",").map(normalize));
  const stored = await env.SEEN.get("prs");
  const list = stored ? JSON.parse(stored)
    : (env.PRS || "").split(",").map(s => s.trim()).filter(Boolean);
  if (!list.length || !env.INGEST_URL || !env.INGEST_TOKEN) return;
  const marks = await readMap(env, "pr_watermarks");
  const legacy = await env.SEEN.get("pr_since");
  let bootstrap = false;
  for (const item of list) {
    if (!marks[item]) {
      marks[item] = legacy || new Date(now.getTime() - 20 * 60000).toISOString();
      bootstrap = true;
    }
  }
  if (bootstrap) await env.SEEN.put("pr_watermarks", JSON.stringify(marks));
  const events = [];
  const completed = [];
  // Rotate when a long PR list exhausts the per-run budget, preventing starvation.
  const offset = Math.floor(now.getTime() / 60000) % list.length;
  const ordered = list.slice(offset).concat(list.slice(0, offset));
  for (const item of ordered) {
    // Reserve one request for sending each completed event batch.
    if (budget.remaining <= Math.ceil(events.length / 100)) break;
    const [repo, numRaw] = item.split("#");
    const number = Number(numRaw);
    const repoKey = repo ? normalize(repo) : "";
    if (!repoKey || !roster.has(repoKey) || !Number.isInteger(number) || number <= 0) continue;
    try {
      const since = overlap(marks[item] || legacy, now, 10);
      const comments = await ghPages(`https://api.github.com/repos/${repo}/issues/${number}`
        + `/comments?since=${encodeURIComponent(since)}&per_page=100`, env.GH_TOKEN, budget);
      for (const c of comments) {
        const author = (c.user && c.user.login) || "";
        if (author === env.MY_LOGIN) continue;
        events.push({ repo, number, comment_id: c.id, author,
          created_at: c.created_at, body: (c.body || "").slice(0, 4000) });
      }
      completed.push(item);
    } catch (e) { /* Preserve this PR's cursor; others may progress. */ }
  }
  for (let offset = 0; offset < events.length; offset += 100) {
    const response = await request(env.INGEST_URL.replace(/\/issues$/, "/pr-comments"), {
      method: "POST", headers: { "Content-Type": "application/json",
        "X-Ingest-Token": env.INGEST_TOKEN },
      body: JSON.stringify({ events: events.slice(offset, offset + 100) }),
    }, budget);
    if (!response.ok) return;
  }
  if (now.getUTCMinutes() % 10 === 0) {
    await checkpoint(env, "pr_watermarks", marks, completed, now);
  }
}
