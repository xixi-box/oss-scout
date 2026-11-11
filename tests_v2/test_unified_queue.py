import datetime as dt
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest

from dashboard import data
from scout_v2 import state_store
from scout_v2.state_store import ClaimLedger, staged_candidate_count
from scout_v2.workers import auto_pipeline as pipeline


@pytest.fixture
def queue_root(tmp_path, monkeypatch):
    now = dt.datetime.fromisoformat("2026-11-12T02:00:00+08:00")
    monkeypatch.setattr(state_store, "business_now", lambda *a: now)
    monkeypatch.setattr(pipeline, "business_now", lambda *a: now)
    eng = SimpleNamespace(tz_name="Asia/Shanghai", max_claims_per_day=5, repo_lane=lambda _: "open")
    monkeypatch.setattr(pipeline.PolicyEngine, "load", lambda _: (eng, None))
    (tmp_path / "queue").mkdir()
    (tmp_path / "queue/candidates.json").write_text("{}")
    return tmp_path


def read_queue(root):
    return json.loads((root / "queue/candidates.json").read_text(encoding="utf-8"))


def write_queue(root, entries):
    (root / "queue/candidates.json").write_text(json.dumps(entries), encoding="utf-8")


def test_migration_unifies_old_states_keeps_fifo_and_never_touches_writers(queue_root):
    entries = {f"a/b#{i}": {"status": "new" if i != 2 else "recheck_failed",
                            "discovered_at": f"2026-11-11T10:{i:02d}:00+08:00"}
               for i in range(1, 12)}
    entries["a/b#20"] = {"status": "staged", "dispatch_started_at": "2026-11-12T01:59:00+08:00"}
    entries["a/b#21"] = {"status": "running", "custom": "preserve"}
    entries["a/b#22"] = {"status": "pr_open"}
    ledger = ClaimLedger(queue_root / "state/scout_v2.sqlite3")
    assert ledger.reserve_publication("Asia/Shanghai", 5, "job-protected", "a/b",
                                      "fork:fix/issue-20-abcdef0", "x/y")
    ledger.close()
    write_queue(queue_root, entries)
    assert set(data.prune_candidate_backlog(queue_root)) == {"a/b#10", "a/b#11"}
    updated = read_queue(queue_root)
    assert staged_candidate_count(updated) == 10
    assert updated["a/b#20"] == entries["a/b#20"]
    assert updated["a/b#21"] == entries["a/b#21"]
    assert updated["a/b#22"] == entries["a/b#22"]
    assert not any(e["status"] in {"new", "recheck_failed"} for e in updated.values())
    assert updated["a/b#1"]["staged_at"] == entries["a/b#1"]["discovered_at"]
    assert pipeline._eligible(updated["a/b#1"])
    assert pipeline._fine_rescreen_due(updated["a/b#1"])
    assert data.prune_candidate_backlog(queue_root) == []


def test_waiting_reuses_only_same_business_day_fine_screen(queue_root, monkeypatch):
    entry = {"screened_at": "2026-11-11T16:01:00Z"}
    pipeline._wait_entry(entry, queue_root)
    assert entry["status"] == "staged" and pipeline._eligible(entry)
    assert not pipeline._fine_rescreen_due(entry)
    monkeypatch.setattr(pipeline, "business_now", lambda *a: dt.datetime.fromisoformat(
        "2026-11-13T00:00:00+08:00"))
    assert pipeline._fine_rescreen_due(entry)


def test_refill_uses_one_fifo_but_never_bypasses_retry_or_nonquota_due_time(queue_root):
    quota = {"status": "staged", "staged_at": "2026-11-12T01:00:00+08:00",
             "eligible_at": "2026-11-13T04:00:00+08:00", "defer_reason": "daily_quota"}
    earlier = {"status": "staged", "staged_at": "2026-11-11T16:30:00Z",
               "eligible_at": "2026-11-12T00:30:00+08:00", "defer_reason": "execution_wait"}
    scheduled = {**quota, "defer_reason": "execution_wait"}
    items = [("quota", quota), ("earlier", earlier), ("scheduled", scheduled)]
    assert [k for k, _ in pipeline._order_candidates(items)] == ["earlier"]
    assert [k for k, _ in pipeline._order_candidates(items, refill_day="2026-11-12")] == ["earlier", "quota"]
    quota["screen_retry_at"] = "2026-11-12T02:05:00+08:00"
    assert [k for k, _ in pipeline._order_candidates(items, refill_day="2026-11-12")] == ["earlier"]


def test_concurrent_screening_cannot_overfill_the_waiting_queue(queue_root, monkeypatch):
    write_queue(queue_root, {f"a/b#{i}": {"status": "staged"} for i in range(10, 19)})
    barrier = Barrier(2)

    def score(*a, **kw):
        barrier.wait(timeout=10)
        return [], {"usable": True, "reason": "approved"}

    monkeypatch.setattr(data, "screen_issue", score)
    ds = SimpleNamespace(root=queue_root)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda n: data.ingest_cloudflare_issues(
            ds, [{"repo": "a/b", "number": n}]), (1, 2)))
    assert sum(n for n, _ in results) == 1
    assert staged_candidate_count(read_queue(queue_root)) == 10


def test_actions_queue_keeps_its_waiting_slot_until_real_start(queue_root, monkeypatch):
    entries = {f"a/b#{i}": {"status": "staged"} for i in range(10)}
    write_queue(queue_root, entries)
    calls = []
    started = "2026-11-12T02:01:00+08:00"

    class Backend:
        def poll(self, remote_id):
            calls.append(remote_id)
            q = read_queue(queue_root)
            if len(calls) <= 2:
                assert q["a/b#0"]["status"] == "staged"
                assert staged_candidate_count(q) == 10
                return {"state": "running", "started_at": started if len(calls) == 2 else None}
            assert q["a/b#0"]["status"] == "running"
            assert q["a/b#0"]["started_at"] == started
            assert staged_candidate_count(q) == 9
            return {"state": "succeeded"}

        def collect(self, remote_id):
            return {"outcome": "succeeded"}

    monkeypatch.setattr(pipeline.time, "sleep", lambda _: None)
    pipeline._collect(queue_root, "github_actions", "run", {
        "backend_obj": Backend(), "candidate_id": "a/b#0"})


def test_expiration_never_changes_a_dispatched_waiter(queue_root):
    old = {"status": "staged", "staged_at": "2026-11-01T00:00:00+08:00"}
    protected = {**old, "dispatch_started_at": "2026-11-12T01:59:00+08:00"}
    write_queue(queue_root, {"a/b#1": old, "a/b#2": protected})
    assert pipeline.expire_staged(queue_root) == 1
    assert read_queue(queue_root)["a/b#1"]["status"] == "stage_expired"
    assert read_queue(queue_root)["a/b#2"] == protected


def test_migration_removes_expired_waiters_before_selecting_the_oldest_ten(queue_root):
    entries = {f"a/b#{i}": {"status": "staged", "staged_at": f"2026-11-12T01:{i:02d}:00+08:00"}
               for i in range(1, 12)}
    entries["a/b#old"] = {"status": "new", "discovered_at": "2020-01-01T00:00:00+08:00"}
    write_queue(queue_root, entries)
    assert data.prune_candidate_backlog(queue_root) == ["a/b#11"]
    queue = read_queue(queue_root)
    assert queue["a/b#old"]["status"] == "stage_expired"
    assert staged_candidate_count(queue) == 10
