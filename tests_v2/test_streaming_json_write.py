import json
from pathlib import Path

import pytest

from scout_v2 import state_store as store


def test_large_queue_write_streams_and_preserves_exact_previous_bytes(tmp_path, monkeypatch):
    path = tmp_path / "queue/candidates.json"
    path.parent.mkdir()
    previous = '{"prior": "中文🌍"}\n'.encode("utf-8")
    path.write_bytes(previous)
    payload = {str(n): {"status": "staged", "body": "数据🌍" * 100} for n in range(150)}
    # Persistence may not allocate another complete serialized or parsed queue.
    dumps, loads = json.dumps, json.loads
    def bounded_dump(value, *args, **kwargs):
        if value is payload:
            pytest.fail("full serialization")
        return dumps(value, *args, **kwargs)
    def bounded_load(value, *args, **kwargs):
        if len(value) > 10000:
            pytest.fail("full verification parse")
        return loads(value, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(store.json, "dumps", bounded_dump)
        patch.setattr(store.json, "loads", bounded_load)
        store.atomic_json_write(path, payload)
    assert json.loads(path.read_text(encoding="utf-8")) == payload
    assert path.with_suffix(".json.bak").read_bytes() == previous


def test_valid_but_changed_json_after_replace_is_detected(tmp_path, monkeypatch):
    path = tmp_path / "data.json"
    store.atomic_json_write(path, {"old": 1})
    def corrupt(src, dst):
        Path(dst).write_text('{"wrong": 1}')
    monkeypatch.setattr(store.os, "replace", corrupt)
    with pytest.raises(ValueError, match="checksum"):
        store.atomic_json_write(path, {"new": 2})
    assert json.loads(path.with_suffix(".json.bak").read_text()) == {"old": 1}


def test_encoding_failure_never_replaces_original_or_keeps_tempfile(tmp_path):
    path = tmp_path / "data.json"
    store.atomic_json_write(path, {"old": 1})
    with pytest.raises(TypeError):
        store.atomic_json_write(path, {"new": {1, 2}})
    assert json.loads(path.read_text()) == {"old": 1}
    assert not list(tmp_path.glob("*.tmp"))
