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
// Warm cursors avoid re-reading and re-delivering the entire 30-minute window
// on each tick. KV remains the durable recovery checkpoint across isolate loss.
const warmMarks = Object.create(null);
export default {
  async fetch(request, env) {
    return internalRead(request, env);
  },
  scheduled(event, env, ctx) {
    // Different isolates may replay a window. Downstream deduplication and
    // transactional claims handle that; skip overlapping scans in this isolate.
    if (!activePoll) {
      activePoll = pollOnce(env, warmMarks).finally(() => { activePoll = undefined; });
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
  // The SQLite-backed outbox accepts each issue version durably before advancing
  // a repository watermark. Delivery and retry no longer depend on a VPS pull.
  if (env.INGRESS && issues.length) {
    const stub = env.INGRESS.get(env.INGRESS.idFromName("issue-ingress-v1"));
    for (let offset = 0; offset < issues.length; offset += 20) {
      const response = await stub.fetch("https://internal/stage", {
        method: "POST", body: JSON.stringify({issues: issues.slice(offset, offset + 20)}),
      });
      if (!response.ok) throw new Error("durable ingress failed");
    }
    return;
  }
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

async function pollIssues(env, now, budget, memory = {}) {
  const repos = (env.REPOS || "").split(",").map(s => s.trim()).filter(Boolean);
  if (!repos.length) return;
  const marks = await readMap(env, "watermarks");
  // On durable checkpoint ticks scan from the old KV cursor as before; this
  // catches delayed indexing missed by a warm isolate's shorter overlap.
  if (now.getUTCMinutes() % 30 !== 0) {
    for (const repo of repos) {
      if (Date.parse(memory[repo]) > (Date.parse(marks[repo]) || 0)) {
        marks[repo] = new Date(Date.parse(memory[repo]) - 4 * 60000).toISOString();
      }
    }
  }
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
  // Advance only after successful direct delivery or durable KV fallback.
  for (const repo of completed) memory[repo] = now.toISOString();
  if (now.getUTCMinutes() % 30 === 0) {
    await checkpoint(env, "watermarks", marks, completed, now);
    await env.SEEN.put("diag", JSON.stringify({ at: now.toISOString(),
      ok_repos: completed.length, errors: errors.slice(0, 10), found: issues.length,
      repos: repos.length, has_token: Boolean(env.GH_TOKEN) }));
  }
}

async function pollOnce(env, memory) {
  await pollIssues(env, new Date(), { remaining: 18 }, memory);
}


// Authenticated, read-only edge data collection. No arbitrary URLs, GitHub
// mutations, model credentials or authority over quotas/publication.
function jsonResponse(body, status = 200, headers = {}) {
  return new Response(JSON.stringify(body), { status, headers: {
    "Content-Type": "application/json", "Cache-Control": "no-store", ...headers,
  } });
}
function equalToken(expected, actual) {
  if (!expected || !actual || expected.length !== actual.length) return false;
  let diff = 0;
  for (let i = 0; i < expected.length; i++) diff |= expected.charCodeAt(i) ^ actual.charCodeAt(i);
  return diff === 0;
}
class EdgeRateLimit extends Error {
  constructor(seconds) { super("GitHub rate limited"); this.seconds = seconds; }
}
async function edgeGithub(env, path, budget, payload, optional = false) {
  if (budget.remaining <= 0) throw new Error("edge read budget exhausted");
  budget.remaining--;
  const response = await fetch("https://api.github.com" + path, {
    method: payload ? "POST" : "GET",
    headers: { ...ghHeaders(env.GH_TOKEN), ...(payload ? {"Content-Type":"application/json"} : {}) },
    ...(payload ? {body: JSON.stringify(payload)} : {}),
    redirect: "error", signal: AbortSignal.timeout(15000),
  });
  const result = await response.json();
  const errors = result && result.errors || [];
  const limited = response.status === 429 ||
    (response.status === 403 && (response.headers.get("x-ratelimit-remaining") === "0" ||
      /rate limit/i.test(result?.message || ""))) ||
    (Array.isArray(errors) && errors.some(e => e.type === "RATE_LIMITED" || /rate limit/i.test(e.message || "")));
  if (limited) {
    const retry = Number(response.headers.get("retry-after")) || 0;
    const reset = response.headers.get("x-ratelimit-remaining") === "0"
      ? Number(response.headers.get("x-ratelimit-reset")) - Date.now() / 1000 : 0;
    throw new EdgeRateLimit(Math.max(60, Math.ceil(retry), Math.ceil(reset || 0)));
  }
  if (optional && response.status === 404) return null;
  if (!response.ok || errors.length || !result) throw new Error("incomplete GitHub evidence");
  return result;
}
async function edgePages(env, path, budget) {
  const rows = [];
  for (let page = 1; ; page++) {
    const batch = await edgeGithub(env, path + "?per_page=100&page=" + page, budget);
    if (!Array.isArray(batch) || batch.length > 100) throw new Error("invalid GitHub page");
    rows.push(...batch);
    if (batch.length < 100) return rows;
  }
}
async function edgeIssueContext(env, body, budget) {
  const repo = body.repo;
  const number = body.number;
  const roster = (env.REPOS || "").split(",").map(s => s.trim().toLowerCase());
  if (typeof repo !== "string" || !/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repo) ||
      !roster.includes(repo.toLowerCase()) || !Number.isSafeInteger(number) || number < 1) {
    return jsonResponse({error: "invalid or unmonitored issue"}, 400);
  }
  const path = "/repos/" + repo + "/issues/" + number;
  const [issue, comments, timeline] = await Promise.all([
    edgeGithub(env, path, budget), edgePages(env, path + "/comments", budget),
    edgePages(env, path + "/timeline", budget),
  ]);
  if (issue.number !== number || !["open","closed"].includes(issue.state) ||
      typeof issue.comments !== "number" || issue.comments !== comments.length) {
    throw new Error("issue changed or incomplete comments");
  }
  return jsonResponse({issue, comments, timeline, complete: true});
}
const PR_FRAGMENT = "fragment ScoutPr on PullRequest { repository { nameWithOwner } number title url state createdAt updatedAt closedAt }";
const PR_FULL = "query($cursor: String) { viewer { login pullRequests(first:100,after:$cursor,orderBy:{field:CREATED_AT,direction:DESC}) { totalCount pageInfo { hasNextPage endCursor } nodes { ...ScoutPr } } } } " + PR_FRAGMENT;
const PR_DELTA = "query($cursor: String,$terms:String!) { viewer { login } search(query:$terms,type:ISSUE,first:100,after:$cursor) { totalCount:issueCount pageInfo { hasNextPage endCursor } nodes { ...ScoutPr } } } " + PR_FRAGMENT;
async function edgePrSnapshot(env, body, budget) {
  const cutoff = body.cutoff;
  if (cutoff !== null && (typeof cutoff !== "string" ||
      !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(cutoff) || !Number.isFinite(Date.parse(cutoff)))) {
    return jsonResponse({error:"invalid cutoff"},400);
  }
  let cursor = null, total = null;
  const nodes = [], cursors = new Set(), keys = new Set();
  for (;;) {
    const variables = {cursor};
    if (cutoff !== null) variables.terms = "is:pr author:xixi-box updated:>=" + cutoff;
    const result = await edgeGithub(env, "/graphql", budget, {
      query: cutoff === null ? PR_FULL : PR_DELTA, variables,
    });
    if (result.data?.viewer?.login?.toLowerCase() !== "xixi-box") {
      throw new Error("GitHub PR author token mismatch");
    }
    const page = cutoff === null ? result.data.viewer.pullRequests : result.data.search;
    if (!page || !Array.isArray(page.nodes) || !Number.isSafeInteger(page.totalCount) ||
        (total !== null && total !== page.totalCount)) throw new Error("incomplete PR snapshot");
    total = page.totalCount;
    for (const node of page.nodes) {
      if (!node?.repository?.nameWithOwner || !Number.isSafeInteger(node.number) ||
          !["OPEN","CLOSED","MERGED"].includes(node.state)) throw new Error("invalid PR node");
      const key = node.repository.nameWithOwner.toLowerCase() + "#" + node.number;
      if (keys.has(key)) throw new Error("duplicate PR node");
      keys.add(key); nodes.push(node);
    }
    if (!page.pageInfo?.hasNextPage) break;
    cursor = page.pageInfo.endCursor;
    if (!cursor || cursors.has(cursor) || !page.nodes.length) throw new Error("PR pagination stalled");
    cursors.add(cursor);
  }
  if (nodes.length !== total) throw new Error("incomplete PR snapshot");
  return jsonResponse({totalCount: total, nodes, pageInfo: {hasNextPage: false, endCursor: null}});
}
function monitoredRepo(env, repo) {
  return typeof repo === "string" && /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repo)
    && (env.TRACKED_REPOS || env.REPOS || "").split(",").some(name => name.trim().toLowerCase() === repo.toLowerCase());
}
const POLICY_PATHS = {
  security: ["SECURITY.md", ".github/SECURITY.md", "docs/SECURITY.md"],
  contributing: ["CONTRIBUTING.md", ".github/CONTRIBUTING.md", "docs/CONTRIBUTING.md", "CONTRIBUTING.rst", "CONTRIBUTING"],
};
async function edgeDocument(env, repo, path, ref, budget) {
  const suffix = ref ? "?ref=" + encodeURIComponent(ref) : "";
  const doc = await edgeGithub(env, "/repos/" + repo + "/contents/" + path + suffix, budget, undefined, true);
  if (doc === null) return null;
  if (doc.type !== "file" || doc.encoding !== "base64" || typeof doc.sha !== "string" || !doc.sha ||
      !Number.isSafeInteger(doc.size) || doc.size < 0 || doc.size > 1000000 || typeof doc.content !== "string")
    throw new Error("incomplete policy document");
  const bytes = Uint8Array.from(atob(doc.content.replace(/\s+/g, "")), c => c.charCodeAt(0));
  if (bytes.length !== doc.size) throw new Error("policy document length mismatch");
  return {repository:repo, path, sha:doc.sha, content:new TextDecoder("utf-8",{fatal:true}).decode(bytes)};
}
async function edgeRepositoryPolicy(env, body, budget) {
  const repo = body.repo;
  if (!monitoredRepo(env, repo)) return jsonResponse({error:"invalid or unmonitored repository"},400);
  const meta = await edgeGithub(env, "/repos/" + repo, budget);
  if (typeof meta.default_branch !== "string" || !meta.default_branch) throw new Error("missing default branch");
  const head = await edgeGithub(env, "/repos/" + repo + "/commits/" + encodeURIComponent(meta.default_branch), budget);
  if (!/^(?:[a-f0-9]{40}|[a-f0-9]{64})$/.test(head.sha || "")) throw new Error("missing policy revision");
  const results = await Promise.all(Object.entries(POLICY_PATHS).map(async ([kind, paths]) => {
    for (const path of paths) {
      const found = await edgeDocument(env, repo, path, head.sha, budget);
      if (found) return {kind, ...found};
    }
    const inherited = await edgeDocument(env, repo.split("/")[0] + "/.github",
      kind === "security" ? "SECURITY.md" : "CONTRIBUTING.md", null, budget);
    return inherited ? {kind, ...inherited} : {kind, absent:true};
  }));
  return jsonResponse({complete:true, documents:results.filter(x=>!x.absent),
    confirmed_absent:results.filter(x=>x.absent).map(x=>x.kind)});
}
async function edgeCompetitionSearch(env, body, budget) {
  const {repo, terms} = body;
  if (!monitoredRepo(env, repo) || !Array.isArray(terms) || !terms.length || terms.length > 4 ||
      terms.some(t=>typeof t!=="string" || !/^[A-Za-z0-9_.-]{1,300}$/.test(t)) ||
      new Set(terms).size !== terms.length)
    return jsonResponse({error:"invalid competition query"},400);
  const results = await Promise.all(terms.map(async term => {
    const query = "repo:" + repo + " is:pr is:open " + term;
    const response = await edgeGithub(env, "/search/issues?q=" + encodeURIComponent(query) + "&per_page=10", budget);
    if (!Array.isArray(response.items) || response.incomplete_results === true)
      throw new Error("incomplete competition search");
    return {term, items:response.items};
  }));
  return jsonResponse({complete:true, results});
}
async function edgeReference(env, body, budget) {
  const {repo, number} = body;
  if (!monitoredRepo(env, repo) || !Number.isSafeInteger(number) || number < 1)
    return jsonResponse({error:"invalid or unmonitored reference"},400);
  const pull = await edgeGithub(env, "/repos/" + repo + "/pulls/" + number, budget, undefined, true);
  if (pull !== null) {
    if (!["open","closed"].includes(pull.state) || !Object.hasOwn(pull,"merged_at") || pull.number !== number)
      throw new Error("incomplete reference PR");
    return jsonResponse({complete:true, fact:{repo,number,kind:"pull_request",state:pull.state,merged:Boolean(pull.merged_at),
      url:pull.html_url ?? null,base_ref:pull.base?.ref ?? null,head_sha:pull.head?.sha ?? null}});
  }
  const issue = await edgeGithub(env, "/repos/" + repo + "/issues/" + number, budget);
  if (issue.pull_request || !["open","closed"].includes(issue.state) || issue.number !== number)
    throw new Error("incomplete reference issue");
  return jsonResponse({complete:true,fact:{repo,number,kind:"issue",state:issue.state,
    state_reason:issue.state_reason ?? null,url:issue.html_url ?? null}});
}
// Business table reconciliation reads. Only short-lived tenant tokens are
// forwarded to the fixed Feishu origin; no model settings or write endpoint.
const BUSINESS_FIELDS = {
  "扫描发现": ["键","发现时间","更新时间","最终结果","粗筛结果","细筛结果"],
  "PR 记录": ["键","标题","PR 状态","发现时间","创建时间"],
};
function feishuNorm(value) {
  if (value === undefined) return null;
  if (Array.isArray(value)) {
    if (value.some(seg=>seg && typeof seg==="object" && seg.type==="url" && seg.link))
      return value.map(seg=>Object.fromEntries(["type","text","link"].filter(k=>Object.hasOwn(seg,k)).map(k=>[k,seg[k]])));
    const parts=value.filter(seg=>seg && typeof seg==="object" && seg.text!=null).map(seg=>String(seg.text||""));
    return parts.length?parts.join(""):value;
  }
  if (value && typeof value==="object" && Object.hasOwn(value,"text") && Object.hasOwn(value,"link"))
    return {text:value.text||"",link:value.link||""};
  return value;
}
async function edgeFeishuIndex(env, body, budget, request) {
  const {app_token:app,table_id:table,table_name:name}=body;
  const token=request.headers.get("X-Feishu-Tenant-Token");
  if (!token || token.length>4096 || !/^[A-Za-z0-9]+$/.test(app||"") ||
      !/^tbl[A-Za-z0-9]+$/.test(table||"") || !Object.hasOwn(BUSINESS_FIELDS,name))
    return jsonResponse({error:"invalid business table request"},400);
  const base="https://open.feishu.cn/open-apis/bitable/v1/apps/"+app+"/tables";
  async function get(path) {
    if (budget.remaining--<=0) throw new Error("Feishu read budget exhausted");
    const response=await fetch(base+path,{headers:{"Authorization":"Bearer "+token,"Content-Type":"application/json"},
      redirect:"error",signal:AbortSignal.timeout(15000)});
    if (response.status===429) throw new EdgeRateLimit(Math.max(5,Number(response.headers.get("Retry-After"))||30));
    const result=await response.json();
    if (!response.ok || result.code!==0 || !result.data) throw new Error("Feishu read incomplete");
    return result.data;
  }
  // Verify the actual table name, rather than trusting caller-supplied names.
  const metadata=await get("?page_size=100");
  if (metadata.has_more || !Array.isArray(metadata.items) ||
      !metadata.items.some(row=>row.table_id===table && row.name===name))
    throw new Error("business table binding not confirmed");
  const rows=[], seen=new Set(), cursors=new Set();
  let cursor=null;
  for (;;) {
    const params=new URLSearchParams({page_size:"500",text_field_as_array:"true"});
    if(cursor) params.set("page_token",cursor);
    const page=await get("/"+table+"/records?"+params);
    if(!Array.isArray(page.items) || page.items.length>500) throw new Error("invalid Feishu page");
    for(const record of page.items) {
      const raw=record.fields||{};
      const value=raw["键"];
      const key=Array.isArray(value)?value.map(seg=>String(seg?.text||"")).join(""):String(value||"");
      if(!key) continue;
      if(typeof record.record_id!=="string" || !record.record_id || seen.has(key))
        throw new Error("invalid or duplicate Feishu row");
      seen.add(key);
      rows.push({key,record_id:record.record_id,fields:Object.fromEntries(BUSINESS_FIELDS[name].map(field=>[field,feishuNorm(raw[field])]))});
    }
    if(!page.has_more) break;
    cursor=page.page_token;
    if(!cursor || cursors.has(cursor)) throw new Error("Feishu cursor stalled");
    cursors.add(cursor);
  }
  return jsonResponse({complete:true,rows});
}
const EDGE_READ_ROUTES = {
  "/internal/v1/issue-context":edgeIssueContext, "/internal/v1/pr-snapshot":edgePrSnapshot,
  "/internal/v1/repository-policy":edgeRepositoryPolicy, "/internal/v1/competition-search":edgeCompetitionSearch,
  "/internal/v1/reference-fact":edgeReference, "/internal/v1/feishu-record-index":edgeFeishuIndex,
};
async function internalRead(request, env) {
  const path = new URL(request.url).pathname;
  if (request.method !== "POST" || !Object.hasOwn(EDGE_READ_ROUTES, path))
    return jsonResponse({error:"not found"},404);
  if (!equalToken(env.INGEST_TOKEN, request.headers.get("X-Ingest-Token")))
    return jsonResponse({error:"forbidden"},403);
  if (!env.GH_TOKEN) return jsonResponse({error:"GitHub token unavailable"},503);
  try {
    const raw = await request.text();
    if (raw.length > 4096) return jsonResponse({error:"request too large"},413);
    const body = JSON.parse(raw);
    if (!body || typeof body !== "object" || Array.isArray(body)) return jsonResponse({error:"bad request"},400);
    const budget = {remaining: 36};
    return await EDGE_READ_ROUTES[path](env, body, budget, request);
  } catch (error) {
    if (error instanceof EdgeRateLimit)
      return jsonResponse({error:"rate limited"},429,{"Retry-After":String(error.seconds)});
    return jsonResponse({error:"edge read unavailable"},503);
  }
}


// Durable delivery boundary. The VPS remains the authoritative consumer and
// deduplicates retries by issue version. No publication authority lives here.
export class IssueIngress {
  constructor(state, env) {
    this.state = state;
    this.env = env;
    this.sql = state.storage.sql;
    this.sql.exec("CREATE TABLE IF NOT EXISTS outbox (key TEXT PRIMARY KEY, payload TEXT NOT NULL, created INTEGER NOT NULL, sent INTEGER)");
    this.sql.exec("CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(sent,created)");
    this.flushing = null;
  }
  async fetch(request) {
    if (request.method !== "POST" || new URL(request.url).pathname !== "/stage")
      return jsonResponse({error:"not found"},404);
    const {issues} = await request.json();
    if (!Array.isArray(issues) || !issues.length || issues.length > 20)
      return jsonResponse({error:"invalid batch"},400);
    const entries = [];
    for (const issue of issues) {
      if (!issue || typeof issue.repo !== "string" || !Number.isSafeInteger(issue.number) || issue.number < 1)
        return jsonResponse({error:"invalid issue"},400);
      const payload = JSON.stringify(issue);
      if (new TextEncoder().encode(payload).length > 256 * 1024)
        return jsonResponse({error:"issue too large"},413);
      const bytes = await crypto.subtle.digest("SHA-256",new TextEncoder().encode(payload));
      const key = Array.from(new Uint8Array(bytes), b => b.toString(16).padStart(2,"0")).join("");
      entries.push([key,payload]);
    }
    // Install an alarm before accepting writes; input gates serialize storage
    // operations. The alarm also survives a lost Worker response or restart.
    const alarm = await this.state.storage.getAlarm();
    if (alarm === null) await this.state.storage.setAlarm(Date.now() + 1000);
    this.state.storage.transactionSync(() => {
      for (const [key,payload] of entries)
        this.sql.exec("INSERT OR IGNORE INTO outbox(key,payload,created) VALUES(?,?,?)",key,payload,Date.now());
    });
    // Alarm owns delivery; this request returns only after durable insertion.
    return jsonResponse({stored:entries.length});
  }
  async alarm() {
    if (this.flushing) return this.flushing;
    this.flushing = this.flush().finally(() => {this.flushing = null;});
    return this.flushing;
  }
  async flush() {
    // Schedule a fallback before any network await; failed sends retain rows.
    await this.state.storage.setAlarm(Date.now() + 60000);
    for (let n=0;n<4;n++) {
      const rows = this.sql.exec("SELECT key,payload FROM outbox WHERE sent IS NULL ORDER BY created,key LIMIT 20").toArray();
      if (!rows.length) break;
      if (!this.env.INGEST_URL || !this.env.INGEST_TOKEN) throw new Error("ingress destination unavailable");
      const response = await fetch(this.env.INGEST_URL,{
        method:"POST",redirect:"error",signal:AbortSignal.timeout(10000),
        headers:{"Content-Type":"application/json","X-Ingest-Token":this.env.INGEST_TOKEN},
        body:JSON.stringify({issues:rows.map(row=>JSON.parse(row.payload))}),
      });
      if (!response.ok) throw new Error("controller receipt unavailable");
      const result = await response.json();
      if (result.stored !== rows.length) throw new Error("incomplete controller receipt");
      this.state.storage.transactionSync(() => {
        for (const row of rows) this.sql.exec("UPDATE outbox SET sent=?,payload='' WHERE key=?",Date.now(),row.key);
      });
    }
    // Keep version tombstones for seven days to suppress overlapping scans.
    this.sql.exec("DELETE FROM outbox WHERE sent IS NOT NULL AND sent<?",Date.now()-7*86400000);
    const pending = this.sql.exec("SELECT 1 FROM outbox WHERE sent IS NULL LIMIT 1").toArray().length;
    if (pending) await this.state.storage.setAlarm(Date.now()+1000);
    else await this.state.storage.deleteAlarm();
  }
}
