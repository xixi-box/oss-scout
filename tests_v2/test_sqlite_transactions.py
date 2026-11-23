import json
import sqlite3

import pytest

from scout_v2.state_store import IssueInbox, _write_transaction, pending_scan_receipts, queue_transaction


def test_failed_inbox_batch_rolls_back_and_releases_writer(tmp_path):
    inbox = IssueInbox(tmp_path)
    try:
        with pytest.raises(KeyError):
            inbox.put([{"repo": "a/b", "number": 1}, {"repo": "a/b"}])
        assert not inbox.conn.in_transaction
        assert inbox.conn.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 0
        other = IssueInbox(tmp_path)
        try:
            assert other.put([{"repo": "a/b", "number": 2}]) == 1
        finally:
            other.close()
    finally:
        inbox.close()


def test_inbox_preserves_receipt_time_and_gives_each_lane_a_turn(tmp_path):
    inbox = IssueInbox(tmp_path)
    old = {"repo": "a/b", "number": 1, "title": "old"}
    fresh = {"repo": "a/b", "number": 2, "title": "new"}
    try:
        inbox.put([old])
        key, stamp = pending_scan_receipts(tmp_path)[0]
        with pytest.raises(KeyError):
            inbox.put([fresh, {"repo": "a/b"}])
        assert pending_scan_receipts(tmp_path) == [(key, stamp)]
        inbox.finish(key, inbox.take()[2])
        inbox.put([{**old, "title": "updated"}, fresh])
        assert inbox.received_at(key) == stamp
        assert [row[0] for row in pending_scan_receipts(tmp_path)] == ["a/b#2"]
        assert inbox.take(prefer_new=True)[0] == "a/b#2"
        assert inbox.take(prefer_new=False)[0] == key
    finally:
        inbox.close()


def test_legacy_inbox_migration_keeps_unknown_receipt_time(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    with sqlite3.connect(state / "issue_inbox.sqlite3") as conn:
        conn.execute("CREATE TABLE inbox (key TEXT PRIMARY KEY,payload TEXT,version INTEGER,"
                     "ready REAL,lease_until REAL,attempts INTEGER,error TEXT)")
        conn.execute("INSERT INTO inbox VALUES ('a/b#1','{}',1,0,0,0,'')")
    inbox = IssueInbox(tmp_path)
    try:
        assert pending_scan_receipts(tmp_path) == [("a/b#1", None)]
        assert inbox.take()[0] == "a/b#1"
    finally:
        inbox.close()


def test_queue_write_failure_releases_lock_and_keeps_original(tmp_path, monkeypatch):
    from scout_v2 import state_store
    (tmp_path / "queue").mkdir()
    path = tmp_path / "queue/candidates.json"
    path.write_text('{"original": {}}')
    real_write = state_store.atomic_json_write

    def fail(*args, **kwargs):
        raise OSError("disk error")

    monkeypatch.setattr(state_store, "atomic_json_write", fail)
    with pytest.raises(OSError, match="disk error"):
        with queue_transaction(tmp_path) as queue:
            queue["extra"] = {}
    assert json.loads(path.read_text()) == {"original": {}}
    monkeypatch.setattr(state_store, "atomic_json_write", real_write)
    with queue_transaction(tmp_path) as queue:
        queue["recovered"] = {}
    assert "recovered" in json.loads(path.read_text())


def test_busy_log_names_database_operation_and_wait(tmp_path):
    db = tmp_path / "inbox.sqlite3"
    first = sqlite3.connect(db, isolation_level=None)
    second = sqlite3.connect(db, timeout=0.03, isolation_level=None)
    try:
        first.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError, match="database=inbox.sqlite3; operation=test.put"):
            with _write_transaction(second, db, "test.put"):
                pytest.fail("a contended transaction must not run")
        assert not second.in_transaction
        event = json.loads((tmp_path / "worklog.jsonl").read_text())
        assert event["action"] == "sqlite_busy"
        assert event["target"] == "inbox.sqlite3"
        assert event["result"]["wait_seconds"] >= 0.02
        first.rollback()
        with _write_transaction(second, db, "test.recover"):
            second.execute("CREATE TABLE recovered (value TEXT)")
    finally:
        first.close()
        second.close()


def test_queue_compares_nested_changes_without_serializing_noop_snapshots(tmp_path, monkeypatch):
    from scout_v2 import state_store
    (tmp_path / "queue").mkdir()
    path = tmp_path / "queue/candidates.json"
    path.write_text('{"a/b#1": {"status": "staged", "checks": [{"ok": false}]}}')
    def unexpected_dump(*args, **kwargs):
        raise AssertionError("unchanged queue must not serialize a full snapshot")

    with monkeypatch.context() as patcher:
        patcher.setattr(state_store.json, "dumps", unexpected_dump)
        with queue_transaction(tmp_path):
            pass
    assert not path.with_suffix(".json.bak").exists()
    with queue_transaction(tmp_path) as queue:
        queue["a/b#1"]["checks"][0]["ok"] = True
    assert json.loads(path.read_text())["a/b#1"]["checks"][0]["ok"] is True
    assert json.loads(path.with_suffix(".json.bak").read_text())["a/b#1"]["checks"][0]["ok"] is False
    with queue_transaction(tmp_path) as queue:
        queue["a/b#1"]["checks"][0]["ok"] = 1
    assert type(json.loads(path.read_text())["a/b#1"]["checks"][0]["ok"]) is int
