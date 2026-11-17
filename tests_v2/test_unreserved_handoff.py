import json
import sqlite3
from types import SimpleNamespace

import pytest

from scout_v2.state_store import ClaimLedger, business_now
from scout_v2.workers import auto_pipeline as pipeline


def handoff(tmp_path, **extras):
    key = "traefik/traefik#1"
    (tmp_path / "queue").mkdir()
    entry = {"id": key, "repo": "traefik/traefik", "number": 1, "status": "staged",
             "direct_start_reserved_at": business_now().isoformat(), **extras}
    (tmp_path / "queue/candidates.json").write_text(json.dumps({key: entry}))
    return key, entry


@pytest.mark.parametrize("failure", ["policy_error", "policy_exception", "capacity"])
def test_owned_unreserved_handoff_is_released_on_early_failure(tmp_path, monkeypatch, failure):
    key, entry = handoff(tmp_path)
    if failure == "policy_exception":
        def broken(_):
            raise RuntimeError("policy read failed")
        monkeypatch.setattr(pipeline.PolicyEngine, "load", broken)
        with pytest.raises(RuntimeError, match="policy read"):
            pipeline.candidate_pipeline(tmp_path, entry)
    else:
        eng = SimpleNamespace(tz_name="Asia/Shanghai", max_claims_per_day=5)
        monkeypatch.setattr(pipeline.PolicyEngine, "load", lambda _: (eng, "unavailable" if failure == "policy_error" else None))
        if failure == "capacity":
            monkeypatch.setattr(ClaimLedger, "dispatch_capacity", lambda *_: {"available": 0})
        pipeline.candidate_pipeline(tmp_path, entry)
    current = json.loads((tmp_path / "queue/candidates.json").read_text())[key]
    assert current["status"] == "staged" and not current.get("direct_start_reserved_at")


@pytest.mark.parametrize("protected", ["remote_id", "started", "backed"])
def test_unreserved_cleanup_preserves_a_possible_remote_writer(tmp_path, monkeypatch, protected):
    extras = {"dispatch_job_id": "job-old"} if protected == "remote_id" else (
        {"dispatch_started_at": business_now().isoformat()} if protected == "started" else {})
    key, entry = handoff(tmp_path, **extras)
    if protected == "backed":
        ledger = ClaimLedger(tmp_path / "state/scout_v2.sqlite3")
        try:
            assert ledger.reserve_dispatch("Asia/Shanghai", 5, "job-active", entry["repo"], key)
        finally:
            ledger.close()
    monkeypatch.setattr(pipeline.PolicyEngine, "load", lambda _: (None, "unavailable"))
    pipeline.candidate_pipeline(tmp_path, entry)
    current = json.loads((tmp_path / "queue/candidates.json").read_text())[key]
    assert current["direct_start_reserved_at"] == entry["direct_start_reserved_at"]


def test_admission_not_owned_cannot_clear_another_handoff(tmp_path):
    key, entry = handoff(tmp_path)
    (tmp_path / "state").mkdir()
    connection = sqlite3.connect(tmp_path / "state/dispatcher.sqlite3", isolation_level=None)
    connection.execute("BEGIN IMMEDIATE")
    try:
        assert pipeline.candidate_pipeline(tmp_path, entry)["abort"] == "dispatcher busy"
        assert json.loads((tmp_path / "queue/candidates.json").read_text())[key]["direct_start_reserved_at"]
    finally:
        connection.close()


@pytest.mark.parametrize("path", ["policy_failure", "release_direct_start"])
def test_unreserved_direct_release_obeys_latest_fifo_pool_limit(tmp_path, monkeypatch, path):
    from scout_v2.console_models import write_models
    import datetime as dt
    key, entry = handoff(tmp_path, staged_at=(business_now() - dt.timedelta(hours=2)).isoformat())
    write_models(tmp_path, {"candidate_pool_size": "1"})
    queue_path = tmp_path / "queue/candidates.json"
    queue = json.loads(queue_path.read_text())
    queue["traefik/traefik#2"] = {"id": "traefik/traefik#2", "status": "staged",
                                   "staged_at": business_now().isoformat()}
    queue_path.write_text(json.dumps(queue))
    if path == "policy_failure":
        monkeypatch.setattr(pipeline.PolicyEngine, "load", lambda _: (None, "unavailable"))
        pipeline.candidate_pipeline(tmp_path, entry)
    else:
        pipeline.release_direct_start(tmp_path, key)
    current = json.loads(queue_path.read_text(encoding="utf-8"))
    assert current[key]["status"] == "queue_evicted"
    assert current[key]["staged_at"] == entry["staged_at"]
    assert current["traefik/traefik#2"]["status"] == "staged"
    assert sum(item["status"] == "staged" for item in current.values()) == 1
