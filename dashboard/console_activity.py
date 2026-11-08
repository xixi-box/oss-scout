"""Read-only activity for the console preview."""

import datetime as dt
import json
import re
import sqlite3
from subprocess import TimeoutExpired

import yaml
from dashboard.data import GhError
from scout_v2.roster import monitored_repo
from scout_v2.state_store import business_now


def _github(ds, path):
    def read():
        try:
            return json.loads(ds.gh_runner(["api", path]))
        except (GhError, OSError, TimeoutExpired, ValueError):
            return None

    return ds._cached("console:" + path, 5, read)


def _publications(ds, day):
    conn = ds._open_ro()
    if conn is None:
        return [], []
    try:
        pending = conn.execute(
            "SELECT job_id,repo,head,runner_repo,remote_id FROM publications WHERE day IS NULL"
        ).fetchall()
        today = conn.execute("SELECT url FROM publications WHERE day=?", (day,)).fetchall()
        return pending, [url for (url,) in today]
    except sqlite3.OperationalError as exc:
        if "no such table: publications" not in str(exc):
            raise
        return [], []
    finally:
        conn.close()


def _active(run, repo, number, kind, runner_repo):
    if run and run.get("status") == "in_progress" and run.get("run_started_at"):
        return dict(repo=repo, number=number, kind=kind,
                    workflow=f"https://github.com/{runner_repo}/actions/runs/{run['id']}")
    return None


def _running_new_prs(ds, pending):
    running = []
    available = True
    for job_id, repo, head, runner_repo, run_id in pending:
        issue = re.fullmatch(r"[^:]+:fix/issue-(\d+)-[0-9a-f]+-[0-9a-f]+", head)
        if not issue or not monitored_repo(repo):
            continue
        if run_id:
            run = _github(ds, f"repos/{runner_repo}/actions/runs/{run_id}")
            available &= run is not None
        else:
            runs = _github(ds, f"repos/{runner_repo}/actions/workflows/oss-scout-repair.yml/runs?status=in_progress")
            available &= runs is not None
            run = next((r for r in (runs or {}).get("workflow_runs", [])
                        if r.get("display_title") == f"repair {job_id}"), None)
        if run and run.get("display_title") == f"repair {job_id}":
            item = _active(run, repo, int(issue[1]), "created", runner_repo)
            if item:
                running.append(item)

    return running, available


def candidate_activity(ds):
    pending, _ = _publications(ds, business_now().date().isoformat())
    running, _ = _running_new_prs(ds, pending)
    active = {f"{item['repo'].lower()}#{item['number']}" for item in running}
    published = {}
    pr_states = {}
    conn = ds._open_ro()
    if conn is not None:
        try:
            for repo, head, url in conn.execute(
                "SELECT repo,head,url FROM publications WHERE day IS NOT NULL AND url IS NOT NULL ORDER BY day,rowid"
            ):
                issue = re.fullmatch(r"[^:]+:fix/issue-(\d+)-[0-9a-f]+-[0-9a-f]+", head)
                if issue:
                    published[f"{repo.lower()}#{issue[1]}"] = url
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='publication_status'").fetchone():
                pr_states = dict(conn.execute(
                    "SELECT url,state FROM publication_status WHERE state IN ('closed','merged')"
                ))
        except sqlite3.OperationalError as exc:
            if "no such table: publications" not in str(exc):
                raise
        finally:
            conn.close()
    return active, published, pr_states


def snapshot(ds):
    tz = "Asia/Shanghai"
    now = business_now(tz)
    day = now.date().isoformat()

    def timestamp(stamp):
        try:
            parsed = dt.datetime.fromisoformat(stamp or "")
            return (parsed if parsed.tzinfo else parsed.replace(tzinfo=now.tzinfo)).timestamp()
        except (ValueError, TypeError):
            return 0

    pending, urls = _publications(ds, day)
    worklog = ds.worklog()
    records, repairs = [], []
    publication_times = {}
    minute = now.replace(second=0, microsecond=0)
    first_minute = minute - dt.timedelta(minutes=59)
    rows = [dict(time=(first_minute + dt.timedelta(minutes=i)).strftime("%H:%M"),
                 coarse=[], fine=[], pr=[], reply=[], retry=[]) for i in range(60)]
    event_kinds = {"pipeline_coarse_screen": "coarse", "pipeline_fine_screen": "fine",
                   "pipeline_pr_open": "pr", "dashboard_pr_fix_result": "pr",
                   "dashboard_pr_reply_drafted": "reply"}
    for entry in worklog:
        action, result = entry.get("action"), str(entry.get("result") or "")
        stamp = str(entry.get("ts") or "")
        if action == "pipeline_pr_open":
            publication_times[result.split(" ", 1)[0]] = stamp
        target = re.fullmatch(r"([\w.-]+/[\w.-]+)#(\d+)", str(entry.get("target") or ""))
        kind = event_kinds.get(action)
        if kind == "reply" and not result.startswith("drafted=True"):
            kind = None
        if action == "dashboard_pr_fix_result" and not result.startswith("pushed=True "):
            kind = "retry" if result.startswith("pushed=False ") else None
        if kind and target:
            offset = int((timestamp(stamp) - first_minute.timestamp()) // 60)
            if 0 <= offset < len(rows):
                repo, number = target[1], int(target[2])
                link_type = "pull" if kind in ("pr", "reply") else "issues"
                detail = result
                if action == "pipeline_pr_open":
                    pr = re.fullmatch(r"https://github\.com/[\w.-]+/[\w.-]+/pull/(\d+)", result.split(" ", 1)[0])
                    if pr:
                        number, detail = int(pr[1]), "新 PR 已创建"
                elif action == "dashboard_pr_fix_result":
                    link_type, detail = "pull", "修复已推送" if kind == "pr" else "修复未推送"
                elif kind == "reply":
                    detail = "飞书回复草稿已生成"
                rows[offset][kind].append(dict(repo=repo, number=number, type=link_type, detail=detail))
        if action != "dashboard_pr_fix_result" or not result.startswith("pushed=True "):
            continue
        if target and monitored_repo(target[1]):
            repairs.append(dict(repo=target[1], number=int(target[2]), time=stamp))
    for url in urls:
        pr = re.fullmatch(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)", url or "")
        if not pr or not monitored_repo(pr[1]):
            continue
        stamp = publication_times.get(url)
        if not stamp:
            stamp = (_github(ds, f"repos/{pr[1]}/pulls/{pr[2]}") or {}).get("created_at")
        records.append(dict(repo=pr[1], number=int(pr[2]), time=stamp))

    running, available = _running_new_prs(ds, pending)

    try:
        attempts = json.loads((ds.root / "state/pr_fix_attempted.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        attempts = {}
    except (OSError, ValueError):
        attempts = {}
        available = False
    if attempts:
        runner_repo = "xixi-box/oss-scout"
        runs = _github(ds, f"repos/{runner_repo}/actions/workflows/oss-scout-pr-fix.yml/runs?status=in_progress")
        available &= runs is not None
        by_name = {r.get("display_title"): r for r in (runs or {}).get("workflow_runs", [])}
        for key in attempts:
            match = re.fullmatch(r"([\w.-]+/[\w.-]+)#(\d+)@(.+)", key)
            if not match or not monitored_repo(match[1]):
                continue
            repo, number, suffix = match.groups()
            job_id = f"prfix-{repo.replace('/', '-')}-{number}-{suffix[:12]}"
            run = by_name.get(job_id)
            item = _active(run, repo, int(number), "repair", runner_repo)
            if item and any(r["repo"].lower() == repo.lower() and r["number"] == int(number)
                            and timestamp(r["time"]) >= timestamp(run["run_started_at"])
                            for r in repairs):
                continue
            if item:
                running.append(item)
    queue = []
    for key, entry in ds.candidates().items():
        target = re.fullmatch(r"([\w.-]+/[\w.-]+)#(\d+)", key)
        if entry.get("status") == "staged" and target and monitored_repo(target[1]):
            queue.append(dict(repo=target[1], number=int(target[2]),
                              time=entry.get("staged_at"), eligible_at=entry.get("eligible_at")))
    queue.sort(key=lambda item: timestamp(item["time"]))
    try:
        cfg = yaml.safe_load((ds.root / "configs/dashboard.yaml").read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        cfg = {}
    hour = int(cfg.get("staged_flush_hour_local", 4))
    schedule_minute = int(cfg.get("staged_flush_minute_local", 0))
    return dict(working=running, working_available=available, rows=rows, queue=queue,
                schedule=f"{hour:02d}:{schedule_minute:02d}",
                today=sorted(records, key=lambda r: timestamp(r["time"]), reverse=True),
                repairs=sorted(repairs, key=lambda r: timestamp(r["time"]), reverse=True)[:10])
