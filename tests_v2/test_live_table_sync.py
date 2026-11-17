from concurrent.futures import Future, ThreadPoolExecutor
import json
import threading

import pytest

from scout_v2.state_store import ClaimLedger, atomic_json_write, business_now
from scout_v2.table_sync import TABLES, TableSyncSchedule, subscribe


class DeferredExecutor:
    def __init__(self):
        self.calls = []

    def submit(self, sync, name, full):
        future = Future()
        self.calls.append((name, full, future))
        return future

    def finish(self, result=None):
        for _, _, future in self.calls:
            if not future.done():
                future.set_result(result or {"updated": 0, "skipped": 10})


def schedule(root):
    feed = subscribe(root)
    executor = DeferredExecutor()
    sync = TableSyncSchedule(feed, executor, lambda *_: None)
    sync.tick(0)
    executor.finish()
    sync.tick(0.1)
    executor.calls.clear()
    return feed, executor, sync


def test_successful_state_write_wakes_only_affected_tables(tmp_path):
    feed, executor, sync = schedule(tmp_path)
    atomic_json_write(tmp_path / "queue/candidates.json", {"a/b#1": {"status": "running"}})
    assert feed.wake.is_set()
    sync.tick(0.2)
    assert {(name, full) for name, full, _ in executor.calls} == {("scan", False), ("candidates", False)}
    executor.finish()
    sync.tick(0.3)
    executor.calls.clear()
    atomic_json_write(tmp_path / "state/codex/auth.json", {"secret": "private"})
    sync.tick(0.4)
    assert not executor.calls  # Credentials are never table events or payloads.


def test_failed_write_does_not_announce_uncommitted_state(tmp_path, monkeypatch):
    feed = subscribe(tmp_path)
    def failed(*_):
        raise OSError("replace failed")
    monkeypatch.setattr("os.replace", failed)
    with pytest.raises(OSError):
        atomic_json_write(tmp_path / "queue/candidates.json", {})
    assert feed.take() == set()


def test_changes_during_sync_coalesce_without_losing_final_state(tmp_path):
    feed, executor, sync = schedule(tmp_path)
    feed.mark({"candidates"})
    sync.tick(1)
    for _ in range(20):
        feed.mark({"candidates"})
        sync.tick(1.1)
    assert len(executor.calls) == 1
    executor.finish()
    sync.tick(1.2)
    assert len(executor.calls) == 2
    assert executor.calls[-1][:2] == ("candidates", False)


def test_failure_retries_without_other_events_and_keeps_full_reconciliation(tmp_path):
    feed = subscribe(tmp_path)
    executor = DeferredExecutor()
    sync = TableSyncSchedule(feed, executor, lambda *_: None)
    sync.tick(0)
    for name, _, future in executor.calls:
        future.set_result({"error": "Feishu unavailable"} if name == "scan" else {"unchanged": True})
    sync.tick(1)
    count = len(executor.calls)
    sync.tick(5)
    assert len(executor.calls) == count
    sync.tick(6)
    assert executor.calls[-1][:2] == ("scan", True)


def test_fifteen_minute_full_reconciliation_and_thirty_second_pr_poll(tmp_path):
    _, executor, sync = schedule(tmp_path)
    sync.tick(30)
    assert ("pr", False) in {(name, full) for name, full, _ in executor.calls}
    executor.finish()
    sync.tick(31)
    executor.calls.clear()
    sync.tick(900)
    assert {(name, full) for name, full, _ in executor.calls} == {(name, True) for name in TABLES}


def test_external_file_edit_detected_without_in_process_notification(tmp_path):
    feed, executor, sync = schedule(tmp_path)
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs/repositories.yaml").write_text("repositories: {}")
    sync.tick(1)
    assert executor.calls[-1][:2] == ("repos", False)


def test_publication_changes_notify_pr_immediately(tmp_path):
    feed = subscribe(tmp_path)
    ledger = ClaimLedger(tmp_path / "state/scout_v2.sqlite3")
    try:
        assert ledger.reserve_publication("Asia/Shanghai", 10, "job-1", "a/b", "xixi-box:fix/issue-1-abc123", "x/y")
        ledger.finish_publication("job-1", day=business_now().date().isoformat(), url="https://github.com/a/b/pull/2")
        assert feed.take() == {"pr", "scan", "candidates"}
        assert ledger.record_publication_status("https://github.com/a/b/pull/2", "merged", None)
        assert feed.take() == {"pr", "scan", "candidates"}
        assert not ledger.record_publication_status("https://github.com/a/b/pull/2", "merged", None)
        assert not feed.take()
    finally:
        ledger.close()


def test_screening_audit_wakes_scan_without_waiting_for_queue_write(tmp_path):
    from scout_v2.workers.auto_pipeline import log
    feed = subscribe(tmp_path)
    log(tmp_path, "coarse_screen", "a/b#1", "passed")
    assert feed.take() == {"scan"}
    log(tmp_path, "fine_screen", "a/b#1", "passed")
    assert feed.take() == {"scan"}
    log(tmp_path, "crash", "a/b#1", "diagnostic only")
    assert not feed.take()


def test_slow_pr_query_does_not_block_candidate_sync(tmp_path):
    feed = subscribe(tmp_path)
    entered, release, candidate_done = threading.Event(), threading.Event(), threading.Event()
    def sync(name, full):
        if name == "pr":
            entered.set()
            assert release.wait(3)
        if name == "candidates":
            candidate_done.set()
        return {"updated": 1}
    with ThreadPoolExecutor(max_workers=4) as executor:
        coordinator = TableSyncSchedule(feed, executor, sync)
        coordinator.tick(0)
        try:
            assert entered.wait(1)
            assert candidate_done.wait(1)
            assert not coordinator.inflight["pr"][0].done()
        finally:
            release.set()


def test_snapshot_failure_does_not_advance_cache_and_full_reconcile_bypasses_cache(tmp_path, monkeypatch):
    (tmp_path / "state").mkdir()
    from dashboard.app import _sync_bitables
    from scout_v2 import feishu_bitable as fb
    # _sync_bitables loads a fresh module, but all_my_prs uses this shared subprocess module.
    import subprocess
    from types import SimpleNamespace
    monkeypatch.setattr(fb, "configured", lambda _: True)
    available = [True]
    def github(args, **kwargs):
        if not available[0]:
            return SimpleNamespace(returncode=1, stdout="")
        row = {"repository": {"nameWithOwner": "example/backend"}, "number": 2,
               "title": "fix: regression", "url": "https://github.com/example/backend/pull/2",
               "state": "open", "createdAt": "2026-11-17T01:00:00Z"}
        return SimpleNamespace(returncode=0, stdout=json.dumps([row] if "open" in args else []))
    monkeypatch.setattr(subprocess, "run", github)
    writes = []
    monkeypatch.setattr(fb, "sync_table", lambda root, rows, **kw: writes.append((rows, kw)) or {"updated": 1})
    cache = {}
    def sync(full=False):
        return _sync_bitables(tmp_path, activity_cache=cache, table_names={"pr"}, full_reconcile=full)
    assert sync()["pr"]["updated"] == 1
    assert sync()["pr"]["unchanged"] is True
    assert len(writes) == 1
    assert sync(full=True)["pr"]["updated"] == 1
    assert len(writes) == 2
    available[0] = False
    previous = dict(cache)
    assert "error" in sync()["pr"]
    assert cache == previous and len(writes) == 2
    available[0] = True
    # A changed snapshot after a failed fetch still reaches Feishu.
    cache.clear()
    assert sync()["pr"]["updated"] == 1


def test_new_pr_backfill_uses_confirmed_url_and_closed_state_ignores_search_lag(tmp_path, monkeypatch):
    from scripts import monitor_prs as monitor
    ledger = ClaimLedger(tmp_path / "state/scout_v2.sqlite3")
    try:
        assert ledger.reserve_publication("Asia/Shanghai", 10, "job-1", "example/backend",
                                          "xixi-box:fix/issue-1-abc1234-def5678", "xixi-box/oss-scout")
        ledger.finish_publication("job-1", day=business_now().date().isoformat(),
                                  url="https://github.com/example/backend/pull/2")
        monkeypatch.setattr(monitor, "all_my_prs", lambda: [])
        calls = []
        def get(args):
            calls.append(args)
            return json.dumps({"html_url": "https://github.com/example/backend/pull/2", "number": 2,
                               "state": "open", "title": "fix: regression", "merged_at": None})
        monkeypatch.setattr(monitor, "_gh", get)
        row, = monitor.build_pr_rows(tmp_path)
        assert calls == [["api", "repos/example/backend/pulls/2"]]
        monkeypatch.setattr(monitor, "all_my_prs", lambda: [dict(row)])
        ledger.record_publication_status(row["url"], "closed", business_now().date().isoformat())
        assert monitor.build_pr_rows(tmp_path) == []
        # Old closed ledger states must not hide a later reopened PR forever.
        import datetime as dt
        ledger._conn.execute("UPDATE publications SET day=?", ((business_now() - dt.timedelta(days=1)).date().isoformat(),))
        assert monitor.build_pr_rows(tmp_path)[0]["state"] == "open"
    finally:
        ledger.close()


def test_complete_empty_pr_snapshot_can_clear_but_failed_fetch_cannot(tmp_path, monkeypatch):
    (tmp_path / "state").mkdir()
    from dashboard.app import _sync_bitables
    from scout_v2 import feishu_bitable as fb
    import subprocess
    from types import SimpleNamespace
    monkeypatch.setattr(fb, "configured", lambda _: True)
    monkeypatch.setattr(subprocess, "run", lambda *_, **kw: SimpleNamespace(returncode=0, stdout="[]"))
    calls = []
    monkeypatch.setattr(fb, "sync_table", lambda root, rows, **kw: calls.append((rows, kw["allow_empty"])) or {"pruned": 1})
    assert _sync_bitables(tmp_path, table_names={"pr"})["pr"]["pruned"] == 1
    assert calls == [([], True)]
    monkeypatch.setattr(subprocess, "run", lambda *_, **kw: SimpleNamespace(returncode=1, stdout=""))
    assert "error" in _sync_bitables(tmp_path, table_names={"pr"})["pr"]
    assert len(calls) == 1
