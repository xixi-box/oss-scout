"""Controller data access, issue screening and Cloudflare integration."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path

import tenacity

import yaml

class GhError(Exception):
    """The GitHub CLI did not return a successful response."""


@tenacity.retry(
    stop=tenacity.stop_after_attempt(2),
    wait=tenacity.wait_fixed(4),
    retry=tenacity.retry_if_exception_type((subprocess.TimeoutExpired, GhError)),
    reraise=True,
)
def _gh_once(args: list[str]) -> str:
    try:
        p = subprocess.run(["gh", *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=20)
    except subprocess.TimeoutExpired:
        raise
    if p.returncode != 0:
        raise GhError((p.stderr or p.stdout or "").strip()[:300])
    return p.stdout


def default_gh_runner(args: list[str]) -> str:
    """gh 调用，tenacity 带退避重试（本机 TLS 抖动常态）。"""
    return _gh_once(args)


class DataSource:
    """Controller state and cached GitHub reads."""

    def __init__(self, root: Path, gh_runner=None):
        self.root = Path(root)
        self.gh_runner = gh_runner or default_gh_runner
        self._cache: dict[str, tuple[float, object]] = {}
        self._refreshing: set[str] = set()

    # ---------- 缓存 ----------

    FAILURE_TTL = 30.0   # 失败短缓存：gh 抖动时页面秒开，30 秒内自动重试恢复

    def _cached(self, key: str, ttl: float, fn):
        """stale-while-revalidate：未过期回缓存；过期先回旧值 + 后台线程刷新；
        无旧值才同步计算。失败（None）短缓存 30s——gh 抖动不再阻塞每个请求。"""
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit:
            age = now - hit[0]
            if hit[1] is None:
                if age < self.FAILURE_TTL:
                    return None
            elif age < ttl:
                return hit[1]
            # 过期：立即回旧值（含旧失败），后台刷新
            if key not in self._refreshing:
                self._refreshing.add(key)
                import threading
                def _bg():
                    try:
                        v = fn()
                        self._cache[key] = (time.monotonic(), v)
                    finally:
                        self._refreshing.discard(key)
                threading.Thread(target=_bg, daemon=True).start()
            return hit[1]
        value = fn()
        self._cache[key] = (now, value)   # None 也记（30s 失败缓存）
        return value

    def _db_path(self) -> Path:
        return self.root / "state" / "scout_v2.sqlite3"

    def _open_ro(self):
        path = self._db_path()
        if not path.exists():
            return None
        try:
            return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10)
        except sqlite3.Error:
            return None

# ---------- Cloudflare Worker 回传通道（2026-10-25） ----------

def feishu_verification_token(ds) -> str:
    """飞书卡片回调的 verification token。

    来源（按序）：环境变量 FEISHU_VERIFICATION_TOKEN →
    /data/state/feishu_app.json 的 verification_token 字段。
    未配置返回空串——回调接口在未配置时会拒绝所有卡片动作（503），
    防止匿名端点以我们名义发评论。
    """
    tok = (os.environ.get("FEISHU_VERIFICATION_TOKEN") or "").strip()
    if tok:
        return tok
    data_root = os.environ.get("OSSCOUT_DASHBOARD_ROOT", "/data")
    try:
        d = json.loads((Path(data_root) / "state" / "feishu_app.json")
                       .read_text(encoding="utf-8"))
        return str(d.get("verification_token") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def cloudflare_cfg(ds) -> dict:
    """Cloudflare 配置。密钥优先取环境变量，文件里只放非密结构。

    2026-10-31：仓库转公开，configs/cloudflare.yaml 里的 api_token / ingest_token
    是真实凭据，不能留在版本库里。现在文件只保留 account_id、namespace、repos
    这类标识符，token 一律由环境变量注入（GitHub Secrets → 容器）。

    环境变量名：
      CF_API_TOKEN      Cloudflare API token（读写 KV）
      CF_INGEST_TOKEN   Worker → dashboard 的共享密钥
    """
    p = ds.root / "configs" / "cloudflare.yaml"
    try:
        cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    # 环境变量覆盖文件值：文件里理论上已经没有真 token，但保留覆盖能力，
    # 这样运维可以只改容器环境而不动镜像。
    for env_key, cfg_key in (("CF_API_TOKEN", "api_token"),
                             ("CF_INGEST_TOKEN", "ingest_token"),
                             ("CF_ACCOUNT_ID", "account_id"),
                             ("CF_KV_NAMESPACE_ID", "kv_namespace_id")):
        val = (os.environ.get(env_key) or "").strip()
        if val:
            cfg[cfg_key] = val
    return cfg


_RE_OFFER = __import__("re").compile(
    # 报告者主动提出要自己修：英文常见说法（"May I prepare that focused fix?"、
    # "I'd like to work on this"、"happy to send a PR"…）
    r"(may|can|could|shall|should)\s+i\s+(prepare|submit|open|send|provide|fix|take)"
    r"|i('ll|'d| would like to| am happy to| am going to| will)\s+"
    r"(prepare|submit|open|work on|fix|send|provide)"
    r"|(happy|glad|willing)\s+to\s+(prepare|submit|open|send|provide|fix)"
    r"|i\s+(would like to|want to|plan to|intend to)\s+(fix|work on|prepare|take)",
    re.I)


# 更宽的认领/派单/禁 AI 信号（clickhouse#126287 实录）：报告者在 body 里写
# "the fix maybe is not trivial to ask AI to do it"、维护者在评论里派单
# "you can fix this and I can try to review"。Worker 事件只有 body 没有
# 评论列表，先把 body 里命中的句子喂给评分层。
def repo_rules_text(repo: str) -> str:
    """该仓库的 PR 规矩摘要（configs/repo_rules.yaml），供细筛 prompt 注入。"""
    candidates = [Path(os.environ.get("OSSCOUT_DASHBOARD_ROOT", "/data")) / "configs/repo_rules.yaml",
                  Path(__file__).resolve().parents[1] / "configs/repo_rules.yaml",
                  Path(__file__).resolve().parents[1] / "configs.seed/repo_rules.yaml"]
    rule = {}
    for path in candidates:
        if path.exists():
            rules = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            rule = (rules.get("rules") or {}).get(repo) or {}
            break
    note = str(rule.get("note") or "").strip()
    return note if note else "none recorded"


_RE_CLAIMISH = re.compile(
    r"not\s+(?:trivial|easy|suitable|appropriate|realistic)\s+(?:enough\s+)?"
    r"(?:for|to\s+ask|to)\s+(?:an?\s+)?(?:ai|agent|llm|bot)"
    r"|too\s+(?:complex|hard|difficult|involved)\s+for\s+(?:an?\s+)?(?:ai|agent|llm|bot)"
    r"|\b(?:ai|agent|llm|bot)s?\s+(?:cannot|can't|shouldn't|should\s+not|won't)\b"
    r"|(?:you|he|she|they|@\w+)\s+(?:can|could|should|may|will)\s+"
    r"(?:fix|take|handle|work\s+on|look\s+into)\s+(?:this|it|that|the\s)"
    r"|\b(?:i|we)\s*(?:'ll|'d|will|would|am\s+going\s+to|want\s+to|"
    r"plan\s+to|intend\s+to|can|could)\s+"
    r"(?:fix|take|handle|claim|work\s+on|look\s+into|implement|prepare|submit|open|send)"
    r"|working\s+on\s+(?:this|it|that|a\s+fix|the\s+fix)"
    r"|\b(?:opened|submitted|created|filed|pushed)\s+(?:a\s+)?"
    r"(?:pr\b|pull\s?request|fix\b|branch)"
    r"|assign(?:ed|ing)?\s+(?:this|it|that)?\s*(?:to\s+)?@\w+",
    re.I)


def _claimish_sentences(body: str) -> list[str]:
    """body 里命中的认领/派单/禁 AI 句子（至多 5 条，每条截 300 字）。"""
    out: list[str] = []
    for chunk in re.split(r"(?<=[.!?])\s+|\n+", body or ""):
        chunk = chunk.strip()
        if chunk and _RE_CLAIMISH.search(chunk):
            out.append(chunk[:300])
            if len(out) >= 5:
                break
    return out


def refresh_issue(item: dict, *, root: Path | None = None) -> tuple[dict, list[str], list]:
    """Get one live snapshot and its mechanical competition checks."""
    from scout_v2.git_host import recheck_candidate

    check = recheck_candidate(item["repo"], int(item["number"]), use_title_keywords=True)
    if check is None or not check.complete or check.screening_source is None:
        raise RuntimeError(f"issue context unavailable: {check.errors if check else []}")
    issue, comments, timeline = check.screening_source
    if root is not None and getattr(check, "similar_prs", []):
        from scout_v2.workers.auto_pipeline import log
        log(root, "similar_pr", f"{item['repo']}#{item['number']}", "；".join(check.similar_prs))
    return issue_screening_snapshot(item, issue, comments, timeline), live_issue_blockers(check), timeline


def live_issue_blockers(check) -> list[str]:
    blockers = []
    if not check.issue_open:
        blockers.append("Issue 已关闭")
    if check.has_claim_signal:
        blockers.append("claim_signal")
    if check.has_cross_ref_pr or check.open_pr_hits:
        refs = check.open_pr_hits or getattr(check, "linked_pr_urls", [])
        blockers.append("已有修复 PR" + (f" · {refs[0]}" if refs else ""))
    if check.is_duplicate:
        blockers.append("confirmed_duplicate")
    return blockers


def issue_screening_snapshot(item: dict, issue: dict, comments: list, timeline=()) -> dict:
    related_prs = {}
    for event in timeline:
        source = (event.get("source") or {}).get("issue") or {}
        pr = source.get("pull_request")
        if pr and source.get("html_url"):
            related_prs[source["html_url"]] = {
                "url": source["html_url"], "title": source.get("title"),
                "state": "merged" if pr.get("merged_at") else source.get("state"),
                "author": (source.get("user") or {}).get("login"),
            }
    return {**item, "title": issue.get("title") or "", "body_excerpt": issue.get("body") or "",
            "created_at": issue.get("created_at"), "state_reason": issue.get("state_reason"),
            "reporter": (issue.get("user") or {}).get("login"),
            "reporter_association": issue.get("author_association"),
            "reactions": (issue.get("reactions") or {}).get("total_count", 0),
            "updated_at": issue.get("updated_at") or "", "state": issue.get("state"),
            "pull_request": bool(issue.get("pull_request")), "assignees": issue.get("assignees") or [],
            "labels": [l["name"] for l in issue.get("labels") or []],
            "comments": len(comments), "context_complete": True, "related_prs": list(related_prs.values()),
            "issue_comments": [{"author": (c.get("user") or {}).get("login", "?"),
                                "association": c.get("author_association", ""),
                                "url": c.get("html_url"),
                                "created_at": c.get("created_at"), "updated_at": c.get("updated_at"),
                                "body": c.get("body") or ""} for c in comments]}


def coarse_issue_blockers(eng, it: dict, *, lane: str, live_blockers=(), root=None) -> list[str]:
    """Shared mechanical issue rules for intake and the pre-start check."""
    repo = str(it["repo"])
    excluded = {"invalid", "wontfix", "duplicate", "question", "stale", "documentation",
                "awaiting more info", "awaiting-more-info", "needs more info", "needs-repro"}
    labels = [str(l) for l in it.get("labels") or []]
    blockers = list(live_blockers)
    if repo not in (getattr(eng, "repos_cfg", {}).get("repositories") or {}) or lane == "unknown":
        blockers.append("repo_not_actionable")
    elif lane == "observe":
        blockers.append(eng.repo_entry(repo).get("review_reason") or "repo_not_actionable")
    elif lane == "gated":
        blockers.append("仓库要求先沟通")
    elif lane == "restricted" and not (
        it.get("maintainer_confirm") or (it.get("signals") or {}).get("maintainer_confirm")
        or any(label.lower() == "help wanted" for label in labels)
    ):
        blockers.append("等待维护者确认")
    if it.get("pull_request") or it.get("state", "open") != "open":
        blockers.append("not_open_unassigned_issue")
    assignment = issue_assignment(eng, it)
    if assignment["assigned_to_others"]:
        blockers.append("已有指派给其他人")
    elif (assignment["requires_assignment"] and not assignment["assignment_exempt"]
          and not assignment["assigned_to_me"]):
        blockers.append("仓库要求指派给 xixi-box")
    if excluded.intersection(l.lower() for l in labels):
        blockers.append("excluded_label")
    if it.get("state_reason") == "duplicate":
        blockers.append("confirmed_duplicate")
    if root is not None and not blockers:
        from scout_v2.workers.cla import preflight
        cla = preflight(root, repo)
        if cla.get("blocking"):
            blockers.append("需要签署 CLA · " + str(cla.get("detail") or ""))
    return list(dict.fromkeys(blockers))


def issue_assignment(eng, it: dict) -> dict:
    from scout_v2.git_host import assignment_facts

    rule = eng.pr_assignment(it["repo"]) if hasattr(eng, "pr_assignment") else {}
    return assignment_facts(it, rule)


def record_issue_screen(eng, it: dict, action: str, result: str) -> None:
    if getattr(eng, "root", None) is not None:
        from scout_v2.workers.auto_pipeline import log
        log(eng.root, action, f"{it['repo']}#{it['number']}", result)


def screen_issue(eng, it: dict, *, lane: str, live_blockers=(),
                 timeline: list | None = None) -> tuple[list[str], dict]:
    """Apply the mechanical gate before asking OpenJev."""
    from scout_v2.git_host import gh_pages, linked_fix_prs

    blockers = coarse_issue_blockers(eng, it, lane=lane, live_blockers=live_blockers,
                                     root=getattr(eng, "root", None))
    if blockers:
        record_issue_screen(eng, it, "coarse_screen", ",".join(blockers))
        return blockers, {"usable": None, "reason": "not screened"}

    number = int(it["number"])
    if timeline is None:
        timeline = gh_pages(f"repos/{it['repo']}/issues/{number}/timeline", 100) if it.get("context_complete") else []
    if timeline is None:
        reason = "issue timeline unavailable"
        record_issue_screen(eng, it, "coarse_screen", f"待重试 · {reason}")
        return [], {"usable": None, "reason": reason}
    record_issue_screen(eng, it, "coarse_screen", "通过机械条件")
    return [], fine_screen_issue(eng, it, lane=lane, linked_prs=linked_fix_prs(timeline, number))


def fine_screen_issue(eng, it: dict, *, lane: str, linked_prs: list | None = None) -> dict:
    """Evaluate a snapshot that has already passed the mechanical gate."""
    from scout_v2.git_host import own_issue_claim
    from scout_v2.workers.openjev import OpenJevClient

    repo, number = str(it["repo"]), int(it["number"])
    title, body = str(it.get("title") or ""), str(it.get("body_excerpt") or "")
    labels = [str(label) for label in it.get("labels") or []]
    claim_sents = _claimish_sentences(body)
    try:
        reporter = it.get("reporter")
        verdict = OpenJevClient().score_issue({
            "repo": repo, "number": number, "title": title, "labels": labels,
            "body": body, "issue_url": it.get("url"),
            "state": it.get("state"), "state_reason": it.get("state_reason"),
            "created_at": it.get("created_at"), "updated_at": it.get("updated_at"),
            "reporter": reporter, "reporter_association": it.get("reporter_association"),
            "assignees": [a.get("login") for a in it.get("assignees") or []],
            **issue_assignment(eng, it),
            "reactions": it.get("reactions", 0), "lane": lane,
            "num_comments": int(it.get("comments") or 0),
            "comments": [{**c, "is_reporter": bool(reporter and c.get("author") == reporter)}
                         for c in it.get("issue_comments") or []
                         if not own_issue_claim({"user": {"login": c.get("author") or ""},
                                                 "body": c.get("body") or ""})],
            "linked_pull_requests": linked_prs or [],
            "claim_evidence_in_body": claim_sents,
            "reporter_offered_fix": bool(_RE_OFFER.search(body)),
            "claimish_signal": bool(claim_sents),
            "repo_rules": repo_rules_text(repo)})
    except Exception as ex:
        verdict = {"usable": None, "reason": f"scorer error: {ex}"[:200]}
    outcome = ("已跳过" if verdict.get("skipped") else
               {True: "通过", False: "拒绝", None: "待重试"}[verdict.get("usable")])
    record_issue_screen(eng, it, "fine_screen", f"{outcome} · {verdict.get('reason', '')}")
    return verdict


def prune_candidate_backlog(root: Path) -> list[str]:
    """Migrate legacy waiting states and keep the oldest ten; never alter writers."""
    from scout_v2.state_store import (STAGED_CANDIDATE_LIMIT, business_now, queue_transaction,
                                      publication_quota_remaining, candidate_fifo_key)
    from scout_v2.repo_policy import PolicyEngine
    from scout_v2.workers.auto_pipeline import (_defer_entry, _wait_entry, _stage_age_days,
                                               STAGE_TTL_DAYS, log)

    now = business_now()
    eng, error = PolicyEngine.load(root)
    left = publication_quota_remaining(root, eng.tz_name, eng.max_claims_per_day) if not error else 0
    protected = set()
    db = root / "state/scout_v2.sqlite3"
    if db.exists():
        with sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True) as conn:
            try:
                for repo, head in conn.execute("SELECT repo,head FROM publications WHERE day IS NULL"):
                    match = re.search(r":fix/issue-(\d+)-", head)
                    if match:
                        protected.add(f"{repo}#{match[1]}".lower())
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc):
                    raise

    expired = []
    with queue_transaction(root) as queue:
        for key, entry in queue.items():
            if key.lower() in protected or entry.get("dispatch_started_at"):
                protected.add(key.lower())
                continue
            if entry.get("status") in ("new", "recheck_failed"):
                entry.setdefault("staged_at", entry.get("discovered_at") or now.isoformat())
                _wait_entry(entry, root)
                if not left:
                    _defer_entry(entry, root)
            elif entry.get("status") == "staged":
                entry.setdefault("staged_at", entry.get("discovered_at") or now.isoformat())
                entry.setdefault("defer_reason", "daily_quota")
                if not entry.get("eligible_at"):
                    stamp = candidate_fifo_key(key, entry)[0]
                    entered = dt.datetime.fromtimestamp(stamp, now.tzinfo) if stamp else now
                    entry["eligible_at"] = dt.datetime.combine(
                        entered.date() + dt.timedelta(days=1), dt.time(4), tzinfo=now.tzinfo).isoformat()
            if entry.get("status") == "staged" and _stage_age_days(entry) > STAGE_TTL_DAYS:
                entry.update(status="stage_expired", updated_at=now.isoformat())
                expired.append(key)
        waiting = [key for key, entry in queue.items()
                   if entry.get("status") == "staged" and key.lower() not in protected]
        waiting.sort(key=lambda key: candidate_fifo_key(key, queue[key]))
        reserved = sum(key.lower() in protected and e.get("status") == "staged"
                       for key, e in queue.items())
        removed = waiting[max(0, STAGED_CANDIDATE_LIMIT - reserved):]
        for key in removed:
            queue[key].update(status="queue_pruned", updated_at=now.isoformat(timespec="seconds"))
    if expired:
        log(root, "stage_expired", str(len(expired)), "expired before queue consolidation: " + ", ".join(expired[:5]))
    return removed


def ingest_cloudflare_issues(ds, items: list[dict], *, refresh: bool = False) -> tuple[int, list[str]]:
    """Mechanical exclusions, then AI; short atomic writes never enclose network IO."""
    import hashlib
    from scout_v2.state_store import (STAGED_CANDIDATE_LIMIT, STAGED_FULL_REASON, atomic_json_write,
                                      business_now, publication_quota_remaining, queue_transaction,
                                      staged_candidate_count)
    from scout_v2.repo_policy import PolicyEngine
    from scout_v2.workers.auto_pipeline import _defer_entry
    eng, err = PolicyEngine.load(ds.root)
    if err or eng is None:
        raise RuntimeError(f"policy unavailable: {err}")
    seen_path = ds.root / "state/seen.json"
    added = []
    for it in items:
        repo, number = str(it.get("repo") or ""), int(it.get("number") or 0)
        if not repo or number <= 0:
            continue
        key = f"{repo}#{number}"
        live_blockers, timeline = (), None
        if refresh:
            it, live_blockers, timeline = refresh_issue(it, root=ds.root)
        fingerprint_input = [it, live_blockers] if live_blockers else it
        fingerprint = hashlib.sha256(
            json.dumps(fingerprint_input, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        with queue_transaction(ds.root) as q:
            seen = _read_json(seen_path, {})()
            prior = seen.get(key) or {}
            if key in q or (prior.get("fingerprint") == fingerprint and prior.get("status") in
                            ("blocked", "rejected", "archived", "screen_skipped")):
                continue
            full = staged_candidate_count(q) >= STAGED_CANDIDATE_LIMIT
        lane = eng.repo_lane(repo)
        if full:
            blockers, verdict = [STAGED_FULL_REASON], {}
            record_issue_screen(eng, it, "coarse_screen", blockers[0])
        else:
            blockers, verdict = screen_issue(eng, it, lane=lane, live_blockers=live_blockers, timeline=timeline)
        now = business_now().isoformat(timespec="seconds")
        rec = {"first_seen": prior.get("first_seen") or now, "updated_at": now, "fingerprint": fingerprint,
               "status": "blocked" if blockers else "screen_skipped" if verdict.get("skipped") else
                         "queued" if verdict.get("usable") is True else
                         "rejected" if verdict.get("usable") is False else "retry",
               "coarse_reason": ",".join(blockers), "fine_reason": str(verdict.get("reason") or "")[:300]}
        if not blockers:
            rec["ai_verdict"] = verdict
        with queue_transaction(ds.root) as q:
            seen = _read_json(seen_path, {})()
            if key in q:
                continue
            if not blockers and verdict.get("usable") is True:
                stage = publication_quota_remaining(ds.root, eng.tz_name, eng.max_claims_per_day) == 0
                if staged_candidate_count(q) >= STAGED_CANDIDATE_LIMIT:
                    rec.update(status="blocked", coarse_reason=STAGED_FULL_REASON)
                else:
                    q[key] = {**it, "id": key, "repo": repo, "number": number, "lane": lane,
                              "blockers": [], "ai_verdict": verdict,
                              "discovered_at": now, "status": "staged", "lease": None,
                              "source": "cloudflare-worker", "screened_at": now,
                              "url": it.get("url") or f"https://github.com/{repo}/issues/{number}"}
                    from scout_v2.workers.auto_pipeline import _wait_entry
                    _wait_entry(q[key], ds.root)
                    if stage:
                        _defer_entry(q[key], ds.root)
                    added.append(key)
            seen[key] = rec
            atomic_json_write(seen_path, seen)
    return len(added), added


def _read_json(path: Path, default):
    def _fn():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return default
    return _fn



def cloudflare_pull(ds) -> dict:
    """从 CF KV 拉取 pending 批次 → ingest → 删除已处理键。返回摘要。

    用 configs/cloudflare.yaml 的 api_token/account_id/kv_namespace_id；
    任一缺失 → {"error": "not configured"}。网络失败 → {"error": ...}。
    """
    cfg = cloudflare_cfg(ds)
    token, account = cfg.get("api_token") or "", cfg.get("account_id") or ""
    ns = cfg.get("kv_namespace_id") or ""
    if not (token and account and ns):
        return {"error": "not configured"}
    base = f"https://api.cloudflare.com/client/v4/accounts/{account}/storage/kv/namespaces/{ns}"
    headers = {"Authorization": f"Bearer {token}"}
    import urllib.request

    @tenacity.retry(
        stop=tenacity.stop_after_attempt(3),
        wait=tenacity.wait_fixed(4),
        reraise=True,
    )
    def _req(path: str, **kw):
        if not path.startswith("https://"):
            raise ValueError(f"blocked non-https url: {path[:60]}")
        req = urllib.request.Request(path, headers=headers, **kw)
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())

    try:
        keys = [k["name"] for k in
                (_req(f"{base}/keys?prefix=pending:&limit=100").get("result") or [])]
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:200]}
    pulled, ingested = 0, 0
    for key in keys:
        try:
            payload = _req(f"{base}/values/{key}")
        except Exception as e:  # noqa: BLE001
            return {"pulled": pulled, "ingested": ingested, "error": str(e)[:200]}
        pulled += 1
        from scout_v2.state_store import IssueInbox
        inbox = IssueInbox(ds.root)
        try:
            ingested += inbox.put(payload.get("issues") or [])
        finally:
            inbox.close()
        try:
            _req(f"{base}/values/{key}", method="DELETE")
        except Exception:  # noqa: BLE001
            pass   # 删除失败：下轮 seen 去重兜底
    return {"pulled": pulled, "ingested": ingested}


def cloudflare_kv_put(ds, key: str, value: str) -> dict:
    """往 CF KV 写一个键。返回 {"ok": bool, "error": str}。

    KV 免费档 1000 write/天，所以调用方必须自己限流——PR 列表同步是 15 分钟
    一次（96/天），在预算内。
    """
    cfg = cloudflare_cfg(ds)
    token, account = cfg.get("api_token") or "", cfg.get("account_id") or ""
    ns = cfg.get("kv_namespace_id") or ""
    if not (token and account and ns):
        return {"ok": False, "error": "not configured"}
    base = (f"https://api.cloudflare.com/client/v4/accounts/{account}"
            f"/storage/kv/namespaces/{ns}/values/{key}")
    import urllib.request
    try:
        req = urllib.request.Request(
            base, data=value.encode("utf-8"), method="PUT",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "text/plain"})
        with urllib.request.urlopen(req, timeout=20) as r:
            body = json.loads(r.read().decode())
        return {"ok": bool(body.get("success")), "error": ""}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}


# ---------- Controller runtime settings ----------

DASHBOARD_DEFAULTS = {
    # KV 的 list 是独立配额（免费档仅 1000 次/天），不算在读额度里。每次
    # pull 都要 list 一次 pending 键：20s = 4320 次/天会直接打爆配额
    # （2026-11-04 Cloudflare 429 实录）。300s = 288 次/天，占配额不到三成。
    "cf_pull_seconds": 300,
    # 每分钟发现并消费候选（用户裁定 2026-11-05：原本 300s 且与 15 分钟
    # 的 PR 监控共用循环，一轮 PR 扫描要 5-10 分钟，候选消费被挤到几小时一次）。
    # 额度仍是硬上限，超出的由 drain 标 staged 留到次日凌晨批量取。
    "auto_repair_seconds": 60,
    # staged 候选每天几点（本地时区）批量激活。凌晨 4 点与 bug hunter 同档，
    # 避开白天 Actions 高峰。
    "staged_flush_hour_local": 4,
}


def dashboard_settings(ds) -> dict:
    p = ds.root / "configs" / "dashboard.yaml"
    cfg = dict(DASHBOARD_DEFAULTS)
    try:
        cfg.update(yaml.safe_load(p.read_text(encoding="utf-8")) or {})
    except Exception:  # noqa: BLE001
        pass
    return cfg


def cf_pull_seconds(ds) -> int:
    try:
        # 下限 240s：老配置文件里持久化的 20 会在 list 配额上打爆 429，
        # 默认值只对"配置文件里没写这个键"的新实例生效。
        return max(240, int(dashboard_settings(ds)["cf_pull_seconds"]))
    except Exception:  # noqa: BLE001
        return DASHBOARD_DEFAULTS["cf_pull_seconds"]
