"""Read-only GitHub PR snapshots and Feishu table builders."""
from __future__ import annotations
import json
import os
import re
import sqlite3
import subprocess
from pathlib import Path
from scout_v2.state_store import STAGED_FULL_REASON
ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = Path(os.environ.get("OSSCOUT_DASHBOARD_ROOT", str(ROOT)))

def _gh(args: list[str], timeout: int = 90) -> str:
    try:
        r = subprocess.run(["gh"] + args, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return ""
    return r.stdout if r.returncode == 0 else ""



def all_my_prs() -> list[dict]:
    from scout_v2.pr_feed import PullRequestFeed

    return PullRequestFeed().snapshot(full=True)


# 台账口径（用户裁定 2026-11-04）：未合并就关掉的 PR 不进表；自己名下的
# 仓库（xixi-box/*，练习/测试/作业类）和 Deep-Optica 相关仓库不是开源贡献，
# 都剔除。merged 保留——被合并才是贡献的终点证明。
LEDGER_EXCLUDE_STATES = {"closed"}
# 我们自己的 GitHub 账号名下的仓库：xixi-box（机器人）和 wangshun-china（个人）
LEDGER_OWN_OWNERS = {"xixi-box", "wangshun-china"}


def ledger_excludes(row: dict) -> bool:
    if str(row.get("state")) in LEDGER_EXCLUDE_STATES:
        return True
    owner = str(row.get("repo", "")).split("/", 1)[0].lower()
    if owner in LEDGER_OWN_OWNERS:
        return True
    if "optica" in str(row.get("repo", "")).lower():
        return True
    return False


def build_pr_rows(root, *, github_rows=None, read_pr=None) -> list[dict]:
    """Enrich the confirmed GitHub PR snapshot with local discovery times."""
    rows = all_my_prs() if github_rows is None else github_rows

    seen = {}
    queue = {}
    for name, target in (("state/seen.json", seen), ("queue/candidates.json", queue)):
        try:
            target.update(json.loads((root / name).read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    issue_by_pr = {str(entry["pr_url"]).lower(): key for key, entry in queue.items()
                   if entry.get("pr_url")}
    db = root / "state/scout_v2.sqlite3"
    if db.is_file():
        conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            for repo, head, url in conn.execute("SELECT repo,head,url FROM publications WHERE day IS NOT NULL"):
                issue = re.fullmatch(r"[^:]+:fix/issue-(\d+)-[0-9a-f]+(?:-[0-9a-f]+)?", head)
                if issue and url:
                    issue_by_pr.setdefault(url.lower(), f"{repo}#{issue[1]}")
        except sqlite3.OperationalError as exc:
            if "no such table: publications" not in str(exc):
                raise
        finally:
            conn.close()
        # The list can lag behind a successful create/close. Overlay only
        # GitHub-confirmed ledger states, and fetch missing new PRs directly.
        from contextlib import closing
        from scout_v2.state_store import business_now
        with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            try:
                confirmed = conn.execute(
                    "SELECT p.repo,p.url,p.day,s.state FROM publications p "
                    "LEFT JOIN publication_status s ON s.url=p.url WHERE p.day IS NOT NULL AND p.url IS NOT NULL").fetchall()
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc):
                    raise
                confirmed = []
        by_url = {r["url"].lower(): r for r in rows}
        today = business_now().date().isoformat()
        for repo, url, day, state in confirmed:
            row = by_url.get(url.lower())
            if row is None and day == today and state != "closed":
                number = url.rsplit("/", 1)[-1]
                if not number.isascii() or not number.isdecimal():
                    raise RuntimeError("invalid confirmed PR URL")
                if read_pr is None:
                    raw = _gh(["api", f"repos/{repo}/pulls/{number}"])
                    item = json.loads(raw) if raw else {}
                else:
                    item = read_pr(repo, number)
                if item.get("html_url", "").lower() != url.lower() or item.get("state") not in ("open", "closed"):
                    raise RuntimeError("new confirmed PR snapshot unavailable")
                row = {"repo": repo, "number": int(number), "url": url, "title": item["title"],
                       "state": "merged" if item.get("merged_at") else item["state"],
                       "created_at": item.get("created_at"), "updated_at": item.get("updated_at"),
                       "closed_at": item.get("closed_at")}
                rows.append(row)
                by_url[url.lower()] = row
            if row is not None and day == today and state in ("merged", "closed"):
                row["state"] = state
    discovered = {key.lower(): entry.get("discovered_at") for key, entry in queue.items()}
    discovered.update({key.lower(): entry["first_seen"] for key, entry in seen.items() if entry.get("first_seen")})

    for row in rows:
        pr_url = row.get("url") or f"https://github.com/{row['repo']}/pull/{row['number']}"
        row["discovered_at"] = discovered.get(issue_by_pr.get(pr_url.lower(), "").lower())
    return [r for r in rows if not ledger_excludes(r)]



def build_repo_rows(root) -> list[dict]:
    """「仓库池」表的行：监控名单 ⊕ 池审计（语言/stars/最近 push）。

    两个来源都是本地文件（configs/repositories.yaml + state/pool_audit.json），
    零 GitHub 调用——池子变化本来就低频，审计文件跑 audit_pool 时刷新。
    """
    import yaml
    cfg_path = root / "configs" / "repositories.yaml"
    if not cfg_path.exists():
        cfg_path = ROOT / "configs" / "repositories.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    repos = cfg.get("repositories") or {}

    audit: dict = {}
    for path in (root / "state" / "pool_audit.json",
                 ROOT / "state" / "pool_audit.json"):
        try:
            for item in json.loads(path.read_text(encoding="utf-8")) or []:
                audit[str(item.get("repo") or "")] = item
            break
        except (OSError, json.JSONDecodeError):
            continue

    rows = []
    for repo, spec in repos.items():
        spec = spec or {}
        if spec.get("internal"):
            continue
        a = audit.get(repo) or {}
        rows.append({
            "repo": repo,
            "lane": spec.get("lane") or "open",
            "internal": bool(spec.get("internal")),
            "pool": spec.get("pool") or "formal",
            "issue_scan": spec.get("issue_scan", True),
            "purpose": spec.get("purpose") or "",
            "merged_by_us": spec.get("merged_by_us", 0),
            "lang": a.get("lang") or "",
            "stars": a.get("stars"),
            "pushed": a.get("pushed") or "",
            "note": spec.get("note") or "；".join(str(x) for x in (a.get("gate_workflows") or []))[:400],
        })
    return rows


def build_scan_rows(root, running_keys=(), published=None, pr_states=None, *,
                    strict=False, worklog_cache=None, only_keys=None) -> list[dict]:
    """「扫描发现」表的行：seen.json 全量镜像。

    这是发现层的总账——Worker 扫到的每个 issue 都在，含被规则否决和
    低分淘汰的。排序按发现时间倒序，表里最新的在最上面（飞书默认视图
    按插入顺序，所以行的顺序不影响，但便于调试）。
    """
    seen_path = root / "state" / "seen.json"
    if not strict and not seen_path.exists():
        seen_path = ROOT / "state" / "seen.json"
    try:
        seen = json.loads(seen_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        if strict:
            raise
        return []
    queue = {}
    try:
        qpath = root / "queue" / "candidates.json"
        if not strict and not qpath.exists():
            qpath = ROOT / "queue" / "candidates.json"
        queue = json.loads(qpath.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        pass
    except (OSError, json.JSONDecodeError):
        if strict:
            raise

    from scout_v2.feishu_bitable import _timestamp

    # Earlier competition aborts did not save the reason on the candidate.
    historical_reasons = {}
    activity = {}
    actions = (
        "pipeline_coarse_screen", "pipeline_prestart_coarse_screen", "pipeline_fine_screen",
        "pipeline_abort", "pipeline_pr_open", "pipeline_reconciled", "pipeline_publication_status",
        "dashboard_pr_approval_ready", "dashboard_pr_approval_delivery_error",
        "dashboard_pr_followup_no_approval", "dashboard_pr_fix_result", "dashboard_pr_reply_sent",
        "dashboard_pr_reply_failed",
    )
    try:
        with (root / "state" / "worklog.jsonl").open("rb") as worklog:
            stat = os.fstat(worklog.fileno())
            identity = (stat.st_dev, stat.st_ino)
            offset = 0
            if (worklog_cache and worklog_cache["identity"] == identity
                    and worklog_cache["offset"] <= stat.st_size):
                offset = worklog_cache["offset"]
                activity = worklog_cache["activity"]
                historical_reasons = worklog_cache["historical_reasons"]
            worklog.seek(offset)
            markers = tuple(action.encode() for action in actions)
            for line in worklog:
                if not line.endswith(b"\n") and worklog_cache is not None:
                    break  # A writer may still be appending this record.
                offset += len(line)
                if not any(marker in line for marker in markers):
                    continue
                try:
                    event = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                key, reason = str(event.get("target") or ""), str(event.get("result") or "")
                action = event.get("action")
                if action == "pipeline_abort" and reason.startswith("competed:"):
                    historical_reasons[key] = reason
                stamp = event.get("ts")
                if action in actions and (_timestamp(stamp) or 0) > (_timestamp(activity.get(key.lower())) or 0):
                    activity[key.lower()] = stamp
            if worklog_cache is not None:
                worklog_cache.update(identity=identity, offset=offset, activity=activity,
                                     historical_reasons=historical_reasons)
    except FileNotFoundError:
        if worklog_cache is not None:
            worklog_cache.clear()
    except OSError:
        if strict:
            raise

    rows = []
    entries = seen.items() if only_keys is None else ((key, seen[key]) for key in only_keys if key in seen)
    for key, v in entries:
        v = v or {}
        if "#" not in key:
            continue
        candidate = queue.get(key) or {}
        candidate_status = str(candidate.get("status") or "")
        queue_state = queue_display_state(candidate_status, candidate.get("updated_at"))
        pr_url = (published or {}).get(key.lower()) or (
            candidate.get("pr_url") if candidate_status in _QUEUE_GREEN else None
        )
        pr_key = re.fullmatch(r"https://github\.com/([^/]+/[^/]+)/pull/(\d+)", pr_url or "")
        last_activity = max((v.get("first_seen"), v.get("updated_at"), candidate.get("updated_at"),
                             candidate.get("started_at"), candidate.get("staged_at"), activity.get(key.lower()),
                             activity.get((pr_url or "").lower()),
                             activity.get(f"{pr_key[1]}#{pr_key[2]}".lower()) if pr_key else None),
                            key=lambda stamp: _timestamp(stamp) or 0)
        result = candidate_activity_state(key, candidate_status, running_keys, candidate)
        reason = candidate.get("abort_reason")
        coarse = candidate.get("coarse_reason")
        if not coarse and candidate_status in {
                "aborted_competed", "aborted_closed", "aborted_duplicate", "needs_cla",
                "needs_maintainer_confirm", "gated_alignment_only", "observe_only", "out_of_scope"}:
            coarse = reason or historical_reasons.get(key) or _SCAN_OTHER_OUTCOMES.get(candidate_status) or candidate_status
        if candidate_status == "aborted_stale" and candidate.get("blockers"):
            coarse = reason or ", ".join(candidate["blockers"])
        if not result and not pr_url and candidate_status != "screen_skipped":
            if coarse:
                result = f"粗筛淘汰 · {coarse}"
            elif candidate_status in {"fine_blocked", "aborted_stale"}:
                result = f"细筛淘汰 · {reason or candidate_status}"
            elif candidate_status in {"aborted_no_evidence", "aborted_pipeline"}:
                result = f"修复失败 · {reason or candidate_status}"
            elif candidate_status.startswith("aborted_"):
                result = f"开工流程拦截 · {reason or candidate_status}"
            elif candidate_status in _SCAN_OTHER_OUTCOMES:
                label = _SCAN_OTHER_OUTCOMES[candidate_status]
                result = f"{label} · {reason}" if reason else label
            elif candidate_status == "queue_pruned":
                result = "已移出候选队列 · 超出 10 条上限"
            elif v.get("status") == "archived":
                result = "已移出候选队列"
        rows.append({
            "key": key,
            "status": "screen_skipped" if candidate_status == "screen_skipped" else str(v.get("status") or ""),
            # 兼容旧记录：早期只写一个 why，按状态归到对应列
            "coarse_reason": str(coarse or v.get("coarse_reason")
                                 or (v.get("why") if str(v.get("status"))
                                     in ("blocked", "unknown_repo", "too_fresh")
                                     else "") or ""),
            "fine_reason": "未执行" if candidate_status == "coarse_blocked" and not (
                coarse == STAGED_FULL_REASON and (v.get("fine_reason") or candidate.get("fine_reason"))
            ) else str(
                candidate.get("fine_reason") or v.get("fine_reason")
                               or (v.get("why") if str(v.get("status")) == "rejected"
                                   else "") or ""),
            "first_seen": str(v.get("first_seen") or ""),
            "last_activity": last_activity,
            "queue_state": queue_state,
            "final_result": result,
            "pr_url": pr_url,
            "pr_state": (pr_states or {}).get(pr_url),
        })
    rows.sort(key=lambda r: r.get("first_seen") or "", reverse=True)
    return rows


# ---- 候选池/扫描发现的入队状态：黄=排队中，绿=已解决，红=已失效 ----

_QUEUE_YELLOW = {"staged", "needs_maintainer_confirm", "needs_cla",
                 "gated_alignment_only", "running"}
_QUEUE_GREEN = {"pr_open", "merged", "completed"}
_QUEUE_RED = {"aborted_stale", "stage_expired", "escalated", "aborted_competed",
              "aborted_duplicate", "aborted_closed", "aborted_no_evidence",
              "aborted_blocked_design", "aborted_pipeline", "coarse_blocked", "fine_blocked",
              "screen_skipped", "queue_pruned", "queue_evicted"}
QUEUE_EXPIRE_DAYS = 3


def queue_display_state(status: str, updated_at: str = "") -> str:
    """候选的处理状态 → 飞书三色语义。

    黄=排队中（含等确认/等CLA），绿=已解决（PR 已开出），红=已失效
    （stale/竞争失败/尝试超限，或 new/staged 躺了超过 3 天没轮上）。
    """
    status = str(status or "")
    import datetime as _dt
    if status in _QUEUE_GREEN:
        return "已解决"
    if status in _QUEUE_RED:
        return "已失效"
    if status in _QUEUE_YELLOW:
        # Expiration is a real queue transition owned by the dispatcher. An old
        # updated_at must not hide work that was just deferred to tomorrow.
        return "排队中"
    return ""


def recheck_still_worth(key: str, title: str = "") -> tuple[bool, str]:
    """粗筛：这条 staged 候选还值得做吗（凌晨批量用）。

    规则层确定性检查：issue 是否仍 open、无人认领、无关联 PR、不是重复。
    有人做了/已关闭 → (False, 原因)。调用失败保守放行（宁重评不误杀）。
    """
    repo, _, num = key.partition("#")
    if not repo or not num.isdigit():
        return False, "bad key"
    try:
        from scout_v2.git_host import recheck_candidate
        r = recheck_candidate(repo, int(num), root_cause_keywords=title or None)
        if r is None or not r.complete:
            return True, "incomplete recheck (kept)"
        if not r.issue_open:
            return False, f"issue closed (state={r.issue_open})"
        if not r.unassigned:
            return False, "already assigned"
        if r.has_claim_signal:
            return False, "claim signal in comments/body"
        if r.is_duplicate:
            return False, f"duplicate of {r.duplicate_of}"
        if r.has_cross_ref_pr or r.open_pr_hits:
            return False, f"PR exists: {(r.open_pr_hits or ['xref'])[0]}"
        return True, "ok"
    except Exception as e:  # noqa: BLE001
        return True, f"recheck error (kept): {str(e)[:100]}"


_SCAN_OTHER_OUTCOMES = {
    "queue_evicted": "已清除",
    "stage_expired": "暂存已过期",
    "escalated": "已停止",
    "needs_cla": "开工拦截 · 需要签署 CLA",
    "needs_maintainer_confirm": "开工拦截 · 等待维护者确认",
    "gated_alignment_only": "开工拦截 · 仓库要求先沟通",
    "observe_only": "开工拦截 · 仓库仅观察",
    "out_of_scope": "开工拦截 · 仓库不在范围内",
}


def candidate_activity_state(key: str, status: str, running_keys, entry=None) -> str:
    if key.lower() in running_keys and status in {"staged", "running", "escalated", "reconciling"}:
        return running_keys[key.lower()] if isinstance(running_keys, dict) else "执行中"
    if status == "reconciling" or (status == "escalated" and (entry or {}).get("retry_authorized_job_id")):
        return "自动对账中"
    if status == "running":
        return "状态核查中"
    if status == "staged":
        if (entry or {}).get("dispatch_job_id") or (entry or {}).get("direct_start_reserved_at"):
            return "准备中"
        return "暂存"
    return ""


def build_candidate_rows(root, running_keys=()) -> list[dict]:
    """候选池只展示可执行、暂存或正在派发/执行的候选。"""
    qpath = root / "queue" / "candidates.json"
    try:
        queue = json.loads(qpath.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    rows = []
    for key, entry in sorted(queue.items()):
        display = candidate_activity_state(key, entry.get("status"), running_keys, entry)
        if not display:
            continue
        rows.append({
            "key": key,
            "title": str(entry.get("title") or "")[:300],
            "queue_state": display,
            "discovered_at": entry.get("discovered_at"),
            "staged_at": entry.get("staged_at"),
        })
    return rows
