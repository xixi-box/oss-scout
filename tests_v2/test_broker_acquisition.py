"""Lost broker acknowledgements must not create duplicate leases or abort reviews."""
import json
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from scout_v2 import codex_auth as auth
from scout_v2.workers.codex_client import CodexCommunicationError
from test_codex_communication_retry import heartbeat_client


def login(root):
    auth.install_auth(root, {"tokens": {"access_token": "private-access",
        "refresh_token": "private-refresh", "account_id": "account-1"}})


def test_lost_acquire_ack_reuses_one_persisted_lease(tmp_path):
    login(tmp_path)
    request = {"operation": "acquire", "protocol": 2, "request_id": uuid.uuid4().hex}
    first, status = auth.exchange(tmp_path, request)
    assert status == 200
    second, status = auth.exchange(tmp_path, request)
    assert status == 200 and second["lease"] == first["lease"]
    assert len(json.loads((tmp_path / "state/codex/leases.json").read_text())) == 1


def test_acquire_timeout_retries_before_starting_codex(tmp_path, monkeypatch):
    client, calls, clock, cleanup = heartbeat_client(tmp_path, monkeypatch, 0)
    real = client._exchange
    acquire_requests = []

    def flaky(operation, **values):
        if operation == "acquire":
            acquire_requests.append(values)
            if len(acquire_requests) == 1:
                clock[0] += 16
                raise CodexCommunicationError("operation=acquire; kind=TimeoutError; elapsed_seconds=15.93")
        return real(operation, **values)

    monkeypatch.setattr(client, "_exchange", flaky)
    ok, detail = client.run("test#medium", "fix")
    assert ok, detail
    assert len(acquire_requests) == 2
    assert acquire_requests[0]["request_id"] == acquire_requests[1]["request_id"]
    assert cleanup == [True]


def test_repeated_release_is_safe_after_lost_response(tmp_path):
    login(tmp_path)
    lease, _ = auth.exchange(tmp_path, {"operation": "acquire", "protocol": 2})
    release = {"operation": "release", "protocol": 2, "lease": lease["lease"]}
    assert auth.exchange(tmp_path, release)[1] == 200
    assert auth.exchange(tmp_path, release)[1] == 200


def test_concurrent_replayed_acquisitions_share_one_lease(tmp_path):
    login(tmp_path)
    body = {"operation": "acquire", "protocol": 2, "request_id": uuid.uuid4().hex}
    with ThreadPoolExecutor(max_workers=6) as workers:
        replies = list(workers.map(lambda _: auth.exchange(tmp_path, body), range(6)))
    assert all(status == 200 for _, status in replies)
    assert len({reply["lease"] for reply, _ in replies}) == 1


def test_expired_acquire_ack_cannot_revive_old_lease(tmp_path, monkeypatch):
    login(tmp_path)
    clock = [1000.0]
    monkeypatch.setattr(auth.time, "time", lambda: clock[0])
    body = {"operation": "acquire", "protocol": 2, "request_id": uuid.uuid4().hex}
    first, _ = auth.exchange(tmp_path, body)
    clock[0] += auth.LEASE_TTL_SECONDS + 1
    second, _ = auth.exchange(tmp_path, body)
    assert second["lease"] != first["lease"]
    assert auth.exchange(tmp_path, {"operation": "sync", "protocol": 2, "lease": first["lease"]})[1] == 409


def test_persistent_acquire_timeout_is_bounded_and_never_starts_codex(tmp_path, monkeypatch):
    client, calls, clock, cleanup = heartbeat_client(tmp_path, monkeypatch, 0)
    attempts = []

    def unavailable(operation, **values):
        attempts.append(values)
        clock[0] += values["request_timeout"]
        raise CodexCommunicationError("operation=acquire; kind=TimeoutError")

    monkeypatch.setattr(client, "_exchange", unavailable)
    ok, detail = client.run("test#medium", "fix")
    assert not ok and "operation=acquire" in detail
    assert 1 < len(attempts) <= 3 and clock[0] <= 90
    assert len({entry["request_id"] for entry in attempts}) == 1
    assert cleanup == [False]


@pytest.mark.parametrize("error", [RuntimeError("Codex login broker returned 401"),
    CodexCommunicationError("operation=acquire; identity changed", retryable=False)])
def test_permanent_acquire_errors_are_not_retried(tmp_path, monkeypatch, error):
    client, calls, clock, cleanup = heartbeat_client(tmp_path, monkeypatch, 0)
    attempts = []

    def rejected(operation, **values):
        attempts.append(operation)
        raise error

    monkeypatch.setattr(client, "_exchange", rejected)
    assert client.run("test#medium", "fix")[0] is False
    assert attempts == ["acquire"] and clock[0] == 0


def test_broker_lock_busy_returns_retryable_503(tmp_path, monkeypatch):
    import sqlite3
    from fastapi.testclient import TestClient
    from dashboard.app import create_app

    monkeypatch.setenv("OSSCOUT_DISABLE_AUTO_REPAIR", "1")
    monkeypatch.setenv("CF_INGEST_TOKEN", "worker-token")

    def busy(*args):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(auth, "exchange", busy)
    with TestClient(create_app(tmp_path, auth_enabled=False)) as client:
        response = client.post("/api/ingest/codex-auth", json={"operation": "acquire"},
                               headers={"X-Ingest-Token": "worker-token"})
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "2"


def test_lost_successful_server_response_is_retried_without_duplicate_lease(tmp_path, monkeypatch):
    login(tmp_path)
    client, calls, clock, cleanup = heartbeat_client(tmp_path, monkeypatch, 0)
    received = []

    def exchange(operation, **values):
        reply, status = auth.exchange(tmp_path, {"operation": operation, **values})
        assert status == 200
        received.append(reply["lease"])
        if len(received) == 1:
            clock[0] += 16
            raise CodexCommunicationError("operation=acquire; kind=TimeoutError")
        return reply

    monkeypatch.setattr(client, "_exchange", exchange)
    reply, _ = client._lease_request("acquire", protocol=2, budget=90, attempts=3, record=lambda *args, **kw: None)
    assert received == [reply["lease"], reply["lease"]]
    assert len(json.loads((tmp_path / "state/codex/leases.json").read_text())) == 1


def test_quarantined_login_does_not_retry_or_clear_security_state(tmp_path, monkeypatch):
    import io
    import urllib.error
    from scout_v2.workers import codex_client as module
    from scout_v2.workers.codex_client import CodexClient

    monkeypatch.setenv("DASHBOARD_URL", "https://controller.example")
    monkeypatch.setenv("CF_INGEST_TOKEN", "private-ingest-token")
    calls = []

    def unavailable(*args, **kwargs):
        calls.append(True)
        raise urllib.error.HTTPError("https://controller.example", 503, "unavailable", {},
                                     io.BytesIO(b'{"retryable": false}'))

    monkeypatch.setattr(module.urllib.request, "urlopen", unavailable)
    with pytest.raises(RuntimeError, match="explicit credential recovery"):
        CodexClient(timeout_seconds=300, workdir=tmp_path)._lease_request(
            "acquire", protocol=2, budget=90, attempts=3, record=lambda *args, **kw: None)
    assert len(calls) == 1


def test_correlated_request_diagnostics_do_not_include_login_material(tmp_path, monkeypatch, caplog):
    login(tmp_path)
    request_id = uuid.uuid4().hex
    clock = [0.0]
    monkeypatch.setattr(auth.time, "monotonic", lambda: clock[0])
    real_write = auth.atomic_json_write

    def slow(*args, **kwargs):
        clock[0] += 2
        return real_write(*args, **kwargs)

    monkeypatch.setattr(auth, "atomic_json_write", slow)
    assert auth.exchange(tmp_path, {"operation": "acquire", "protocol": 2, "request_id": request_id})[1] == 200
    assert f"request_id={request_id}" in caplog.text
    assert "phase=lease_persist" in caplog.text and "phase=total" in caplog.text
    assert "private-access" not in caplog.text and "private-refresh" not in caplog.text


def test_release_timeout_recovers_without_changing_successful_result(tmp_path, monkeypatch):
    client, calls, clock, cleanup = heartbeat_client(tmp_path, monkeypatch, 0)
    real = client._exchange
    releases = []

    def lost_ack(operation, **values):
        response = real(operation, **values)
        if operation == "release":
            releases.append(values["request_id"])
            if len(releases) == 1:
                clock[0] += 10
                raise CodexCommunicationError("operation=release; kind=TimeoutError")
        return response

    monkeypatch.setattr(client, "_exchange", lost_ack)
    ok, detail = client.run("test#medium", "fix")
    assert ok, detail
    assert len(releases) == 2 and len(set(releases)) == 1


def test_invalid_request_id_is_rejected_without_logging_payload(tmp_path, caplog):
    login(tmp_path)
    assert auth.exchange(tmp_path, {"operation": "acquire", "protocol": 2,
                                    "request_id": "private-untrusted-input"})[1] == 400
    assert "private-untrusted-input" not in caplog.text
