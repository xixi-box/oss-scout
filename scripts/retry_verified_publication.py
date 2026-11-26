"""Authorize one completed, unpublished rate-limited repair for normal recovery.

Read-only by default. --apply only stages the explicitly named job; it never
reruns Actions, acquires credentials, pushes a branch or creates a PR.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import datetime as dt
import json
from pathlib import Path
import re
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scout_v2.candidate_store import read_candidates
from scout_v2.git_host import gh_read
from scout_v2.state_store import ClaimLedger, Worklog, business_now, queue_transaction, rotate_staged_queue
from scout_v2.workers.delivery_recovery import load_bundle, publication_retry_fields


def authorize_retry(root: Path, job_id: str, *, apply: bool = False) -> dict:
    if not re.fullmatch(r"job-[a-f0-9-]{36}", job_id):
        raise ValueError("an exact original repair UUID is required")
    root = Path(root)
    folder = root / "artifacts/verified-deliveries" / job_id
    manifest = json.loads((folder / "manifest.json").read_bytes())
    repo, base = manifest["job"]["repo"], manifest["job"]["base_sha"]
    number = manifest["publication"]["issue_number"]
    repository = manifest["source_repository"]
    from scout_v2.workers.auto_pipeline import _actions_runner_repo
    if repository != _actions_runner_repo(root):
        raise ValueError("source is outside the configured controller repository")
    source = {"job_id": job_id, "repository": repository, "run_id": str(manifest["source_run"]),
              "patch_sha256": manifest["patch_sha256"], "base_sha": base}
    files, _ = load_bundle(folder, source, repo=repo, base_sha=base, issue_number=number)
    publication = json.loads(files.get("publish.json", b"{}"))
    # Compatibility only for this explicitly requested old job, never a broad
    # migration of failed candidates. Permission/unknown-write errors do not qualify.
    if (publication.get("ok") or publication.get("created") or publication.get("pr")
            or not re.search(r"(?i)^pr create failed:.*(?:API rate limit.*exceeded|secondary rate limit)",
                             str(publication.get("error") or ""))):
        raise ValueError("original publication is not a confirmed rate-limit rejection")
    publication["failure_kind"] = "publication_rate_limited"
    run = gh_read([f"repos/{repository}/actions/runs/{source['run_id']}"])
    if (not isinstance(run, dict) or run.get("status") != "completed"
            or run.get("display_title") != "repair " + job_id
            or run.get("path", "").split("@")[0] != ".github/workflows/oss-scout-repair.yml"):
        raise ValueError("remote repair is not confirmed completed with the same job identity")
    branch = manifest["publication"].get("branch")
    if not isinstance(branch, str) or not re.fullmatch(r"fix/issue-\d+-[a-z0-9-]+", branch):
        raise ValueError("original publication branch is invalid")
    head = "xixi-box:" + branch
    prs = gh_read([f"repos/{repo}/pulls", "-X", "GET", "-f", "state=all", "-f", "head=" + head])
    if not isinstance(prs, list) or prs:
        raise ValueError("original head has a PR or its publication status is unavailable")
    expected = f"{repo}#{number}"
    queue = read_candidates(root, missing_ok=False)
    key = next((key for key in queue if key.lower() == expected.lower()), None)
    entry = queue.get(key) if key else None
    if (not entry or entry.get("status") in ("running", "pr_open", "staged") or entry.get("pr_url")
            or entry.get("repair_job_id") != job_id or entry.get("dispatch_job_id")
            or entry.get("dispatch_started_at") or entry.get("direct_start_reserved_at")
            or entry.get("last_publication_retry_job_id") == job_id):
        raise ValueError("candidate no longer matches the completed terminal repair")
    with closing(sqlite3.connect((root / "state/scout_v2.sqlite3").resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        if connection.execute("SELECT 1 FROM publications WHERE day IS NULL AND (job_id=? OR lower(candidate_id)=?) LIMIT 1",
                              (job_id, key.lower())).fetchone():
            raise ValueError("candidate still has an active preparation or remote writer")
    now = business_now()
    fields = publication_retry_fields(root, entry, publication, job_id=job_id, repo=repo,
        issue_number=number, run_id=source["run_id"], repository=repository, now=now)
    if not fields:
        raise ValueError("retained repair did not qualify for bounded recovery")
    ended = dt.datetime.fromisoformat(run["updated_at"].replace("Z", "+00:00"))
    if ended.tzinfo is None:
        raise ValueError("remote completion timestamp has no timezone")
    due = max(now, ended.astimezone(now.tzinfo) + dt.timedelta(hours=1)).isoformat()
    fields.update(eligible_at=due, screen_retry_at=due, rescreen_pending=True)
    result = {"job_id": job_id, "candidate": key, "source_run": source["run_id"],
              "patch_sha256": source["patch_sha256"], "eligible_at": due, "applied": False,
              "candidate_status": entry["status"], "evicted": [],
              "remote_completed": True, "head_has_pr": False, "requires_new_reservation": True,
              "requires_publication_preflight": True, "verification_mode": "agent"}
    if apply:
        ledger = ClaimLedger(root / "state/scout_v2.sqlite3")
        try:
            if (ledger.pending_publication(job_id) is not None
                    or key.lower() in {item.lower() for item in ledger.reserved_candidates()}):
                raise ValueError("candidate still has an active preparation or remote writer")
            with queue_transaction(root) as current:
                if current.get(key) != entry:
                    raise ValueError("candidate changed during the read-only checks")
                ledger.release_claim(key, "Asia/Shanghai")
                current[key].update(status="staged", updated_at=now.isoformat(), **fields)
                removed = rotate_staged_queue(root, current)
                result.update(candidate_status=current[key]["status"], evicted=removed)
                evicted = [(old, current[old].get("abort_reason", "")) for old in removed]
        finally:
            ledger.close()
        result["applied"] = True
        Worklog(root / "state/worklog.jsonl").append({"ts": now.isoformat(),
            "action": "authorize_verified_publication_retry", "target": key, "result": result})
        from scout_v2.workers.auto_pipeline import log
        for old, reason in evicted:
            log(root, "queue_evicted", old, reason)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--job", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(authorize_retry(args.root, args.job, apply=args.apply), ensure_ascii=False))
