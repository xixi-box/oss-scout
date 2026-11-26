import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

from scout_v2.candidate_store import (
    CANDIDATE_DB, LEGACY_CANDIDATES, get_candidate, migrate_candidates, read_candidates,
)
from scout_v2.state_store import queue_transaction, rotate_staged_queue
from scout_v2.table_sync import TableChanges


def seed(root, rows):
    legacy = root / LEGACY_CANDIDATES
    legacy.parent.mkdir()
    legacy.write_text(json.dumps(rows), encoding="utf-8")
    return legacy


def test_migration_keeps_all_records_and_only_writes_changed_rows(tmp_path):
    entries = {
        "project/a#9": {"status": "running", "lease": {"remote_id": "111"}},
        "project/a#2": {"status": "staged", "staged_at": "2026-11-24T10:00:00+08:00"},
        "project/a#1": {"status": "staged", "fields": [True, 1], "usable": True},
    }
    legacy = seed(tmp_path, entries)
    before = legacy.read_bytes()
    assert migrate_candidates(tmp_path) == len(entries)
    assert list(read_candidates(tmp_path)) == list(entries)
    assert read_candidates(tmp_path) == entries
    assert read_candidates(tmp_path, statuses=("staged",), keys=("project/a#1",),
                           fields=("fields", "usable", "absent")) == {
        "project/a#1": {"fields": [True, 1], "usable": True, "absent": None}
    }

    with closing(sqlite3.connect(tmp_path / CANDIDATE_DB)) as db, db:
        db.executescript("CREATE TABLE writes(key TEXT); "
                         "CREATE TRIGGER updated AFTER UPDATE ON candidates "
                         "BEGIN INSERT INTO writes VALUES (NEW.key); END;")
    with queue_transaction(tmp_path) as queue:
        assert queue["project/a#9"] == entries["project/a#9"]
        queue["project/a#1"]["fields"][0] = 1
        queue["project/a#2"] = dict(queue["project/a#2"])
    with closing(sqlite3.connect(tmp_path / CANDIDATE_DB)) as db:
        assert db.execute("SELECT key FROM writes").fetchall() == [("project/a#1",)]
    assert type(get_candidate(tmp_path, "project/a#1")["fields"][0]) is int
    assert migrate_candidates(tmp_path) is None
    assert legacy.read_bytes() == before


def test_migration_failure_keeps_legacy_authority(tmp_path):
    legacy = seed(tmp_path, {"project/a#1": {"status": "staged"}, "project/a#2": None})
    before = legacy.read_bytes()
    with pytest.raises(AttributeError):
        migrate_candidates(tmp_path)
    assert not (tmp_path / CANDIDATE_DB).exists()
    assert legacy.read_bytes() == before


def test_transactions_serialize_and_preserve_candidate_order(tmp_path):
    seed(tmp_path, {"project/a#9": {"status": "pr_open", "runs": 0},
                    "project/a#3": {"status": "running"}})
    migrate_candidates(tmp_path)
    feed = TableChanges(tmp_path)
    feed.poll_files()
    assert get_candidate(tmp_path, "project/a#9") is not None
    feed.poll_files()
    assert not feed.take()

    def increment(_):
        with queue_transaction(tmp_path) as queue:
            queue["project/a#9"]["runs"] += 1

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(increment, range(16)))
    assert get_candidate(tmp_path, "project/a#9")["runs"] == 16
    feed.poll_files()
    assert {"scan", "candidates"} <= feed.take()
    with pytest.raises(RuntimeError):
        with queue_transaction(tmp_path) as queue:
            queue["project/a#3"]["status"] = "staged"
            raise RuntimeError("failed")
    assert get_candidate(tmp_path, "project/a#3")["status"] == "running"
    with queue_transaction(tmp_path) as queue:
        entry = queue.pop("project/a#9")
        queue["project/a#1"] = {"status": "staged"}
        queue["project/a#9"] = entry
    assert list(read_candidates(tmp_path)) == ["project/a#3", "project/a#1", "project/a#9"]


def test_rolling_capacity_keeps_original_fifo_after_import(tmp_path, monkeypatch):
    from scout_v2 import console_models

    monkeypatch.setattr(console_models, "runtime_limit", lambda *args: 2)
    rows = {
        "project/a#2": {"status": "staged", "staged_at": "2026-11-25T01:05:00+08:00"},
        "project/a#3": {"status": "running", "staged_at": "2026-11-25T01:00:00+08:00"},
        "project/a#1": {"status": "staged", "staged_at": "2026-11-25T01:02:00+08:00"},
    }
    seed(tmp_path, rows)
    migrate_candidates(tmp_path)
    with queue_transaction(tmp_path) as queue:
        queue["project/a#4"] = {"status": "staged", "staged_at": "2026-11-25T01:10:00+08:00"}
        assert rotate_staged_queue(tmp_path, queue) == ["project/a#1"]
    after = read_candidates(tmp_path)
    assert after["project/a#1"]["status"] == "queue_evicted"
    assert after["project/a#1"]["staged_at"] == rows["project/a#1"]["staged_at"]
    assert after["project/a#3"] == rows["project/a#3"]
