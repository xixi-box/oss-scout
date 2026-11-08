"""Resolve current candidate and publication status for Feishu tables."""

import json
import re
import sqlite3
from subprocess import TimeoutExpired

from dashboard.data import GhError
from scout_v2.roster import monitored_repo


def _github(ds, path):
    def read():
        try:
            return json.loads(ds.gh_runner(["api", path]))
        except (GhError, OSError, TimeoutExpired, ValueError):
            return None

    return ds._cached("console:" + path, 5, read)


def _running_candidates(ds, pending):
    running = set()
    for job_id, repo, head, runner_repo, run_id in pending:
        issue = re.fullmatch(r"[^:]+:fix/issue-(\d+)-[0-9a-f]+-[0-9a-f]+", head)
        if not issue or not monitored_repo(repo):
            continue
        if run_id:
            run = _github(ds, f"repos/{runner_repo}/actions/runs/{run_id}")
        else:
            runs = _github(ds, f"repos/{runner_repo}/actions/workflows/oss-scout-repair.yml/runs?status=in_progress")
            run = next((r for r in (runs or {}).get("workflow_runs", [])
                        if r.get("display_title") == f"repair {job_id}"), None)
        if (run and run.get("display_title") == f"repair {job_id}"
                and run.get("status") == "in_progress" and run.get("run_started_at")):
            running.add(f"{repo.lower()}#{int(issue[1])}")
    return running


def candidate_activity(ds):
    pending = []
    published = {}
    pr_states = {}
    conn = ds._open_ro()
    if conn is not None:
        try:
            pending = conn.execute(
                "SELECT job_id,repo,head,runner_repo,remote_id FROM publications WHERE day IS NULL"
            ).fetchall()
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
    return _running_candidates(ds, pending), published, pr_states
