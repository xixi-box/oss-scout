"""Recover abandoned local handoffs without repeating uncertain remote writes."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

from .state_store import ClaimLedger, business_now, candidate_execution_lock, queue_transaction, rotate_staged_queue


def recover_abandoned_handoffs(root: Path, *, protected=()) -> list[str]:
    from .workers.auto_pipeline import _actions_runner_repo, _find_repair_run, log

    root = Path(root)
    path = root / "queue/candidates.json"
    if not path.exists():
        return []
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    protected = {key.lower() for key in protected}
    now = business_now()
    recovered = []
    evicted = []
    for key, entry in snapshot.items():
        stamp = entry.get("direct_start_reserved_at")
        job_id = entry.get("dispatch_job_id")
        if (key.lower() in protected or entry.get("status") != "staged"
                or not (stamp or job_id) or entry.get("dispatch_started_at") or entry.get("pr_url")):
            continue
        # A partial old cleanup can leave only the job id. The candidate's
        # latest mutation bounds its age; discovery/FIFO time cannot prove that
        # a remote handoff was not attempted moments ago.
        age_stamp = stamp or entry.get("updated_at")
        try:
            then = dt.datetime.fromisoformat(age_stamp.replace("Z", "+00:00"))
            then = then.replace(tzinfo=now.tzinfo) if then.tzinfo is None else then
            if (now - then).total_seconds() < 60:
                continue
        except (ValueError, TypeError, AttributeError):
            continue  # A malformed marker needs inspection, not a duplicate writer.
        with candidate_execution_lock(root, key) as owned:
            if not owned:
                continue
            if job_id:
                # No queue/dispatcher/ledger write transaction spans GitHub I/O.
                run, complete = _find_repair_run(_actions_runner_repo(root), job_id)
                if run is not None or not complete:
                    continue
            admission = sqlite3.connect(root / "state/dispatcher.sqlite3", timeout=0, isolation_level=None)
            ledger = None
            try:
                try:
                    admission.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError as exc:
                    if "locked" in str(exc).lower():
                        continue
                    raise
                ledger = ClaimLedger(root / "state/scout_v2.sqlite3")
                if key.lower() in {candidate.lower() for candidate in ledger.reserved_candidates()}:
                    preparation = {(job, candidate.lower()) for job, candidate in ledger.preparations()}
                    if not job_id or (job_id, key.lower()) not in preparation:
                        continue  # A possibly submitted publication is never reclaimed here.
                with queue_transaction(root) as queue:
                    current = queue.get(key) or {}
                    if (current.get("status") != "staged" or current.get("dispatch_started_at")
                            or current.get("pr_url") or current.get("direct_start_reserved_at") != stamp
                            or current.get("dispatch_job_id") != job_id
                            or (not stamp and current.get("updated_at") != age_stamp)):
                        continue
                    if job_id:
                        ledger.release_claim(key)
                        current.update(repair_job_id=job_id, retry_authorized_job_id=job_id,
                                       abandoned_dispatch_job_id=job_id)
                    current.pop("dispatch_job_id", None)
                    current.pop("direct_start_reserved_at", None)
                    current["updated_at"] = now.isoformat()
                    recovered.append(key)
                    evicted.extend((old, queue[old].get("abort_reason", ""))
                                   for old in rotate_staged_queue(root, queue))
                if job_id:
                    ledger.release_preparation(job_id)
            finally:
                if ledger is not None:
                    ledger.close()
                admission.close()
    for key in recovered:
        log(root, "abandoned_handoff_released", key, "no live owner, no submitted reservation, no remote dispatch")
    for key, reason in evicted:
        log(root, "queue_evicted", key, reason)
    return recovered
