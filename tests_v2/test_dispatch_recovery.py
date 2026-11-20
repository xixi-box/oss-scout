import datetime as dt
import json
import sqlite3
import pytest

from scout_v2.dispatch_recovery import recover_abandoned_handoffs
from scout_v2.state_store import ClaimLedger, business_now, candidate_execution_lock
from scout_v2.workers import auto_pipeline as pipeline


def write_handoff(root, *, job_id=None):
    (root / "queue").mkdir(exist_ok=True)
    entry = {"id": "a/b#1", "repo": "a/b", "status": "staged",
             "direct_start_reserved_at": (business_now() - dt.timedelta(minutes=3)).isoformat(),
             "staged_at": "2026-11-17T04:00:00+08:00"}
    if job_id:
        entry["dispatch_job_id"] = job_id
    (root / "queue/candidates.json").write_text(json.dumps({entry["id"]: entry}))
    return entry


def read_handoff(root):
    return json.loads((root / "queue/candidates.json").read_text())["a/b#1"]


def test_unstarted_same_day_marker_released_without_changing_fifo(tmp_path):
    entry = write_handoff(tmp_path)
    assert recover_abandoned_handoffs(tmp_path) == ["a/b#1"]
    actual = read_handoff(tmp_path)
    assert "direct_start_reserved_at" not in actual
    assert actual["staged_at"] == entry["staged_at"]


def test_local_thread_and_sqlite_reservation_are_both_protected(tmp_path):
    write_handoff(tmp_path)
    assert recover_abandoned_handoffs(tmp_path, protected={"a/b#1"}) == []
    with candidate_execution_lock(tmp_path, "a/b#1") as owned:
        assert owned
        assert recover_abandoned_handoffs(tmp_path) == []
    ledger = ClaimLedger(tmp_path / "state/scout_v2.sqlite3")
    try:
        assert ledger.reserve_dispatch("Asia/Shanghai", 10, "job-live", "a/b", "a/b#1")
        assert recover_abandoned_handoffs(tmp_path) == []
    finally:
        ledger.close()
    assert read_handoff(tmp_path)["direct_start_reserved_at"]


def test_remote_unknown_or_existing_is_never_cleared(tmp_path, monkeypatch):
    write_handoff(tmp_path, job_id="job-uncertain")
    monkeypatch.setattr(pipeline, "_actions_runner_repo", lambda _: "xixi-box/oss-scout")
    for answer in ((None, False), ({"status": "running"}, True), ({"status": "completed"}, True)):
        monkeypatch.setattr(pipeline, "_find_repair_run", lambda *_, answer=answer: answer)
        assert recover_abandoned_handoffs(tmp_path) == []
    assert read_handoff(tmp_path)["dispatch_job_id"] == "job-uncertain"


def test_confirmed_unsubmitted_job_releases_claim_and_network_holds_no_sqlite_lock(tmp_path, monkeypatch):
    write_handoff(tmp_path, job_id="job-never-submitted")
    ledger = ClaimLedger(tmp_path / "state/scout_v2.sqlite3")
    assert ledger.try_claim("Asia/Shanghai", "a/b#1")[0]
    monkeypatch.setattr(pipeline, "_actions_runner_repo", lambda _: "xixi-box/oss-scout")

    def lookup(*args):
        for name in ("queue_lock.sqlite3", "dispatcher.sqlite3", "scout_v2.sqlite3"):
            with sqlite3.connect(tmp_path / "state" / name, timeout=0, isolation_level=None) as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.rollback()
        return None, True

    monkeypatch.setattr(pipeline, "_find_repair_run", lookup)
    try:
        assert recover_abandoned_handoffs(tmp_path) == ["a/b#1"]
        actual = read_handoff(tmp_path)
        assert "dispatch_job_id" not in actual
        assert actual["retry_authorized_job_id"] == actual["repair_job_id"] == "job-never-submitted"
        assert ledger._conn.execute("SELECT action FROM claim_days").fetchone() == ("abort",)
    finally:
        ledger.close()


def test_released_direct_handoff_rejoins_full_pool_and_may_be_the_oldest_eviction(tmp_path):
    from scout_v2.console_models import write_models
    write_handoff(tmp_path)
    write_models(tmp_path, {"candidate_pool_size": "1"})
    path = tmp_path / "queue/candidates.json"
    queue = json.loads(path.read_text())
    original = (business_now() - dt.timedelta(hours=2)).isoformat()
    queue["a/b#1"]["staged_at"] = original
    queue["a/b#2"] = {"id": "a/b#2", "status": "staged", "staged_at": business_now().isoformat()}
    path.write_text(json.dumps(queue))
    assert recover_abandoned_handoffs(tmp_path) == ["a/b#1"]
    current = json.loads(path.read_text(encoding="utf-8"))
    assert current["a/b#1"]["status"] == "queue_evicted"
    assert current["a/b#1"]["staged_at"] == original
    assert current["a/b#2"]["status"] == "staged"
    records = [json.loads(line) for line in (tmp_path / "state/worklog.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(row["action"] == "pipeline_queue_evicted" and row["target"] == "a/b#1" for row in records)


@pytest.mark.parametrize("remote", [(None, True), (None, False), ({"status": "running"}, True)])
def test_orphan_job_without_direct_stamp_requires_old_mutation_and_complete_remote_absence(tmp_path, monkeypatch, remote):
    entry = write_handoff(tmp_path, job_id="job-orphan")
    entry["updated_at"] = entry.pop("direct_start_reserved_at")
    (tmp_path / "queue/candidates.json").write_text(json.dumps({entry["id"]: entry}))
    monkeypatch.setattr(pipeline, "_actions_runner_repo", lambda _: "xixi-box/oss-scout")
    monkeypatch.setattr(pipeline, "_find_repair_run", lambda *args: remote)
    assert recover_abandoned_handoffs(tmp_path) == ([entry["id"]] if remote == (None, True) else [])
    assert ("dispatch_job_id" not in read_handoff(tmp_path)) is (remote == (None, True))


@pytest.mark.parametrize("stamp", [None, "broken", "recent"])
def test_orphan_job_with_unknown_or_recent_age_is_not_reclaimed(tmp_path, monkeypatch, stamp):
    entry = write_handoff(tmp_path, job_id="job-orphan")
    entry.pop("direct_start_reserved_at")
    entry["updated_at"] = business_now().isoformat() if stamp == "recent" else stamp
    (tmp_path / "queue/candidates.json").write_text(json.dumps({entry["id"]: entry}))
    monkeypatch.setattr(pipeline, "_find_repair_run", lambda *args: pytest.fail("no absence proof without age"))
    assert recover_abandoned_handoffs(tmp_path) == []


def test_complete_absence_can_recover_without_any_existing_ledger(tmp_path, monkeypatch):
    write_handoff(tmp_path, job_id="job-never-submitted")
    monkeypatch.setattr(pipeline, "_actions_runner_repo", lambda _: "xixi-box/oss-scout")
    monkeypatch.setattr(pipeline, "_find_repair_run", lambda *args: (None, True))
    assert not (tmp_path / "state/scout_v2.sqlite3").exists()
    assert recover_abandoned_handoffs(tmp_path) == ["a/b#1"]
    assert not read_handoff(tmp_path).get("dispatch_job_id")


def test_abandoned_preparation_retained_after_queue_write_failure_can_be_recovered(tmp_path, monkeypatch):
    write_handoff(tmp_path, job_id="job-cleanup-failed")
    ledger = ClaimLedger(tmp_path / "state/scout_v2.sqlite3")
    try:
        assert ledger.reserve_dispatch("Asia/Shanghai", 5, "job-cleanup-failed", "a/b", "a/b#1")
        monkeypatch.setattr(pipeline, "_actions_runner_repo", lambda _: "xixi-box/oss-scout")
        monkeypatch.setattr(pipeline, "_find_repair_run", lambda *args: (None, True))
        assert recover_abandoned_handoffs(tmp_path) == ["a/b#1"]
        assert ledger.preparations() == []
    finally:
        ledger.close()


def test_remote_publication_reservation_remains_protected_even_if_listing_is_empty(tmp_path, monkeypatch):
    write_handoff(tmp_path, job_id="job-dispatching")
    ledger = ClaimLedger(tmp_path / "state/scout_v2.sqlite3")
    try:
        assert ledger.reserve_dispatch("Asia/Shanghai", 5, "job-dispatching", "a/b", "a/b#1")
        assert ledger.reserve_publication("Asia/Shanghai", 5, "job-dispatching", "a/b",
                                          "owner:fix/issue-1-abcdef0-12345678", "xixi-box/oss-scout")
        monkeypatch.setattr(pipeline, "_actions_runner_repo", lambda _: "xixi-box/oss-scout")
        monkeypatch.setattr(pipeline, "_find_repair_run", lambda *args: (None, True))
        assert recover_abandoned_handoffs(tmp_path) == []
        assert read_handoff(tmp_path)["dispatch_job_id"] == "job-dispatching"
    finally:
        ledger.close()


@pytest.mark.parametrize("status", ["coarse_blocked", "fine_blocked", "screen_skipped", "escalated"])
def test_recover_preparation_after_screening_records_final_result(tmp_path, monkeypatch, status):
    entry = write_handoff(tmp_path, job_id="job-screened-out")
    entry.update(status=status, abort_reason="screening decision")
    (tmp_path / "queue/candidates.json").write_text(json.dumps({entry["id"]: entry}))
    monkeypatch.setattr(pipeline, "_actions_runner_repo", lambda _: "xixi-box/oss-scout")
    ledger = ClaimLedger(tmp_path / "state/scout_v2.sqlite3")
    try:
        assert ledger.reserve_dispatch("Asia/Shanghai", 5, entry["dispatch_job_id"], "a/b", entry["id"])
        monkeypatch.setattr(pipeline, "_find_repair_run", lambda *args: (None, False))
        assert recover_abandoned_handoffs(tmp_path) == []
        assert ledger.preparations()

        monkeypatch.setattr(pipeline, "_find_repair_run", lambda *args: (None, True))
        assert recover_abandoned_handoffs(tmp_path) == [entry["id"]]
        assert ledger.preparations() == []
        actual = read_handoff(tmp_path)
        assert actual["status"] == status
        assert actual["abort_reason"] == "screening decision"
        assert "dispatch_job_id" not in actual
        assert "direct_start_reserved_at" not in actual
        assert "retry_authorized_job_id" not in actual
    finally:
        ledger.close()
