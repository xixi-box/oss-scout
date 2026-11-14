"""Concurrent runner credentials keep the OAuth refresh family on the broker."""
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
import copy
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from scout_v2 import codex_auth as auth
from scout_v2.workers.codex_client import CodexClient, _RefreshProxy


def credentials(access="access-0", refresh="real-rotating-refresh-0", account="account-1"):
    return {"tokens": {"access_token": access, "refresh_token": refresh,
                       "id_token": "id-token", "account_id": account}}


def acquire(root):
    result, status = auth.exchange(root, {"operation": "acquire", "protocol": 2})
    assert status == 200
    return result


def broker_client(root):
    client = CodexClient(timeout_seconds=100, workdir=root)
    def exchange(operation, **values):
        value, status = auth.exchange(root, {"operation": operation, **values})
        if status != 200:
            raise RuntimeError(f"Codex login broker returned {status}")
        return value
    client._exchange = exchange
    return client


def _refresh_in_separate_process(arguments):
    from pathlib import Path

    root, lease = arguments
    root = Path(root)

    def rotate(value):
        with (root / "refresh-count.txt").open("a") as stream:
            stream.write("refresh\n")
        time.sleep(0.05)
        return credentials("process-access", "process-refresh")

    auth._refresh = rotate
    return broker_client(root).refresh_credentials(lease)


def test_concurrent_refresh_rotates_once_and_stale_saves_never_replace_authority(tmp_path, monkeypatch):
    original = credentials()
    auth.install_auth(tmp_path, original)
    first, second = acquire(tmp_path), acquire(tmp_path)
    assert first["lease"] != second["lease"]
    assert first["auth"]["tokens"]["refresh_token"] != original["tokens"]["refresh_token"]
    assert second["auth"]["tokens"]["refresh_token"] != original["tokens"]["refresh_token"]
    assert auth.exchange(tmp_path, {"operation": "acquire"})[1] == 409
    calls = []

    def rotate(value):
        calls.append(copy.deepcopy(value))
        time.sleep(0.03)
        return credentials("access-1", "real-rotating-refresh-1")

    monkeypatch.setattr(auth, "_refresh", rotate)
    barrier = threading.Barrier(2)

    def refresh(lease):
        barrier.wait()
        return broker_client(tmp_path).refresh_credentials(lease)

    with ThreadPoolExecutor(2) as workers:
        results = list(workers.map(refresh, (first, second)))
    assert len(calls) == 1 and calls[0] == original
    assert all(result["auth"]["tokens"]["access_token"] == "access-1"
               for result in results)
    assert len({result["generation"] for result in results}) == 1
    for lease in (first, second):
        assert auth.exchange(tmp_path, {"operation": "sync", "protocol": 2,
                                       "lease": lease["lease"], "auth": original})[1] == 200
        assert auth.exchange(tmp_path, {"operation": "release", "protocol": 2,
                                       "lease": lease["lease"], "auth": original})[1] == 200
    assert json.loads(auth.auth_path(tmp_path).read_text()) == credentials("access-1", "real-rotating-refresh-1")


def test_live_legacy_worker_is_preserved_until_it_releases(tmp_path):
    auth.install_auth(tmp_path, credentials())
    legacy, status = auth.exchange(tmp_path, {"operation": "acquire"})
    assert status == 200
    assert auth.exchange(tmp_path, {"operation": "acquire", "protocol": 2})[1] == 409
    assert auth.exchange(tmp_path, {"operation": "sync", "lease": legacy["lease"],
                                   "auth": credentials("legacy-updated", "legacy-rotated")})[1] == 200
    assert auth.exchange(tmp_path, {"operation": "release", "lease": legacy["lease"]})[1] == 200
    assert acquire(tmp_path)["auth"]["tokens"]["access_token"] == "legacy-updated"


def test_refresh_is_serialized_across_broker_processes(tmp_path):
    auth.install_auth(tmp_path, credentials())
    first, second = acquire(tmp_path), acquire(tmp_path)
    with ProcessPoolExecutor(2) as workers:
        responses = list(workers.map(_refresh_in_separate_process,
                                    [(str(tmp_path), first), (str(tmp_path), second)]))
    assert (tmp_path / "refresh-count.txt").read_text().splitlines() == ["refresh"]
    assert all(result["auth"]["tokens"]["access_token"] == "process-access"
               for result in responses)


def test_expired_lease_and_changed_login_cannot_save_or_refresh(tmp_path, monkeypatch):
    auth.install_auth(tmp_path, credentials())
    lease = acquire(tmp_path)
    with pytest.raises(RuntimeError, match="busy"):
        auth.install_auth(tmp_path, credentials(account="another-account"))
    # Defend against an out-of-band administrative replacement as well.
    auth._save_auth(tmp_path, credentials("new-account-access", account="another-account"))
    result, status = auth.exchange(tmp_path, {"operation": "sync", "protocol": 2,
                                            "lease": lease["lease"], "auth": credentials()})
    assert status == 409 and "identity changed" in result["detail"]
    lease = acquire(tmp_path)
    now = time.time()
    monkeypatch.setattr(auth.time, "time", lambda: now + 121)
    assert auth.exchange(tmp_path, {"operation": "refresh", "protocol": 2,
                                   "lease": lease["lease"], "generation": lease["generation"]})[1] == 409
    assert acquire(tmp_path)["lease"] != lease["lease"]


def test_uncertain_network_refresh_preserves_credentials_and_does_not_reuse_token(tmp_path, monkeypatch):
    original = credentials()
    auth.install_auth(tmp_path, original)
    lease = acquire(tmp_path)
    calls = []

    def fail(value):
        calls.append(value)
        raise urllib.error.URLError("a response containing real-rotating-refresh-0")

    monkeypatch.setattr(auth, "_refresh", fail)
    for _ in range(2):
        with pytest.raises(RuntimeError) as failure:
            broker_client(tmp_path).refresh_credentials(lease)
        assert "real-rotating-refresh" not in str(failure.value)
    assert len(calls) == 1
    assert json.loads(auth.auth_path(tmp_path).read_text()) == original
    assert not auth.auth_ready(tmp_path)
    assert auth.exchange(tmp_path, {"operation": "acquire", "protocol": 2})[1] == 503
    assert auth.exchange(tmp_path, {"operation": "sync", "protocol": 2, "lease": lease["lease"]})[1] == 200
    assert auth.exchange(tmp_path, {"operation": "release", "protocol": 2, "lease": lease["lease"]})[1] == 200
    auth.install_auth(tmp_path, credentials("relogged-in", "new-family"))
    assert auth.auth_ready(tmp_path)


def test_loopback_refresh_proxy_requires_opaque_handle_and_returns_only_runner_credentials(tmp_path, monkeypatch):
    auth.install_auth(tmp_path, credentials())
    lease = acquire(tmp_path)
    seen = []
    monkeypatch.setattr(auth, "_refresh", lambda value: credentials("fresh-access", "fresh-real-refresh"))

    class Client(CodexClient):
        def _exchange(self, operation, **values):
            seen.append((operation, values))
            result, status = auth.exchange(tmp_path, {"operation": operation, **values})
            assert status == 200
            return result

    proxy = _RefreshProxy(Client(timeout_seconds=100, workdir=tmp_path), lease, lambda value: None)
    assert proxy.server.server_address[0] == "127.0.0.1"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(handle):
        return opener.open(urllib.request.Request(proxy.url, data=json.dumps(
            {"grant_type": "refresh_token", "refresh_token": handle}).encode(),
            headers={"Content-Type": "application/json"}), timeout=5)

    try:
        with pytest.raises(urllib.error.HTTPError) as rejected:
            request("wrong-handle")
        assert rejected.value.code == 401 and not seen
        with request(lease["auth"]["tokens"]["refresh_token"]) as response:
            body = json.load(response)
            assert response.headers["Cache-Control"] == "no-store"
        assert body == {"access_token": "fresh-access", "id_token": "id-token",
                        "refresh_token": lease["auth"]["tokens"]["refresh_token"]}
        assert "fresh-real-refresh" not in str(body)
        assert json.loads(auth.auth_path(tmp_path).read_text())["tokens"]["refresh_token"] == "fresh-real-refresh"
    finally:
        proxy.close()


def test_upstream_refresh_uses_official_endpoint_and_never_exposes_rejection_body(monkeypatch):
    def reject(request, timeout):
        assert request.full_url == "https://auth.openai.com/oauth/token"
        assert json.loads(request.data)["refresh_token"] == "real-rotating-refresh-0"
        assert timeout == 10
        raise urllib.error.HTTPError(request.full_url, 400, "invalid_grant", {}, None)

    monkeypatch.setattr(auth.urllib.request, "urlopen", reject)
    with pytest.raises(urllib.error.HTTPError):
        auth._refresh(credentials())


def test_v2_client_routes_cli_refresh_and_never_returns_authority_tokens(tmp_path, monkeypatch):
    import subprocess
    from pathlib import Path
    from scout_v2.workers import codex_client as module

    auth.install_auth(tmp_path, credentials())
    monkeypatch.setattr(auth, "_refresh", lambda value: credentials("access-1", "real-rotated-1"))
    client = module.CodexClient(timeout_seconds=100, workdir=tmp_path)
    calls = []

    def exchange(operation, **values):
        calls.append((operation, values))
        response, status = auth.exchange(tmp_path, {"operation": operation, **values})
        assert status == 200
        return response

    class Process:
        pid = 99999999

        def __init__(self, args, **kwargs):
            self.args, self.env = args, kwargs["env"]
            self.home = Path(self.env["CODEX_HOME"])
            self.done, self.waits = False, 0
            value = json.loads((self.home / "auth.json").read_text())
            assert "real-rotating-refresh-0" not in str(value)
            assert self.env["CODEX_REFRESH_TOKEN_URL_OVERRIDE"].startswith("http://127.0.0.1:")
            assert "127.0.0.1" in self.env["NO_PROXY"]

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                value = json.loads((self.home / "auth.json").read_text())
                request = urllib.request.Request(self.env["CODEX_REFRESH_TOKEN_URL_OVERRIDE"],
                    data=json.dumps({"grant_type": "refresh_token",
                                     "refresh_token": value["tokens"]["refresh_token"]}).encode())
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(request, timeout=5) as response:
                    value["tokens"].update(json.load(response))
                (self.home / "auth.json").write_text(json.dumps(value))
                raise subprocess.TimeoutExpired("codex", timeout)
            Path(self.args[self.args.index("-o") + 1]).write_text("completed")
            self.done = True
            return 0

        def poll(self):
            return 0 if self.done else None

    monkeypatch.setattr(client, "_exchange", exchange)
    monkeypatch.setattr(module.subprocess, "Popen", Process)
    assert client.run("model#medium", "fix issue") == (True, "completed")
    assert [operation for operation, _ in calls] == ["acquire", "refresh_begin", "refresh_commit", "sync", "release"]
    assert all("auth" not in body for operation, body in calls if operation != "refresh_commit")
    assert json.loads(auth.auth_path(tmp_path).read_text())["tokens"]["refresh_token"] == "real-rotated-1"


def test_explicit_recovery_two_borrowers_share_one_attempt(tmp_path, monkeypatch):
    auth.install_auth(tmp_path, credentials())
    blocked = auth.auth_path(tmp_path).parent / "refresh-blocked.json"
    auth.atomic_json_write(blocked, {"generation": auth._generation(credentials()), "reason": "VPS timeout"})
    leases = []
    for _ in range(2):
        value, status = auth.exchange(tmp_path, {"operation": "acquire", "protocol": 2, "recover": True})
        assert status == 200
        leases.append(value)
    entered, finish = threading.Event(), threading.Event()
    calls = []
    def rotate(value):
        calls.append(value)
        entered.set()
        assert finish.wait(3)
        return credentials("recovered-access", "recovered-refresh")
    monkeypatch.setattr(auth, "_refresh", rotate)
    with ThreadPoolExecutor(2) as pool:
        owner = pool.submit(broker_client(tmp_path).refresh_credentials, leases[0], True)
        assert entered.wait(3)
        assert not auth.auth_ready(tmp_path)
        # Recovery is already marked attempted, but pending callers may still join.
        assert auth.exchange(tmp_path, {"operation": "acquire", "protocol": 2, "recover": True})[1] == 200
        waiter = pool.submit(broker_client(tmp_path).refresh_credentials, leases[1], True)
        finish.set()
        results = [owner.result(), waiter.result()]
    assert len(calls) == 1
    assert len({row["generation"] for row in results}) == 1
    assert auth.auth_ready(tmp_path) and not blocked.exists()


def test_failed_explicit_recovery_is_not_silently_repeated(tmp_path, monkeypatch):
    auth.install_auth(tmp_path, credentials())
    blocked = auth.auth_path(tmp_path).parent / "refresh-blocked.json"
    auth.atomic_json_write(blocked, {"generation": auth._generation(credentials()), "reason": "VPS timeout"})
    lease, status = auth.exchange(tmp_path, {"operation": "acquire", "protocol": 2, "recover": True})
    assert status == 200
    calls = []
    def fail(value):
        calls.append(value)
        raise urllib.error.HTTPError("https://auth.openai.com/oauth/token", 400, "secret", {}, None)
    monkeypatch.setattr(auth, "_refresh", fail)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            broker_client(tmp_path).refresh_credentials(lease, recover=True)
    assert len(calls) == 1
    marker = json.loads(blocked.read_text())
    assert marker["recovery_attempted"] and marker["failure"] == "HTTPError HTTP 400"
    assert "secret" not in str(marker)
    assert auth.exchange(tmp_path, {"operation": "acquire", "protocol": 2, "recover": True})[1] == 503


def test_lost_commit_response_retries_receipt_without_rotating_again(tmp_path, monkeypatch):
    auth.install_auth(tmp_path, credentials())
    lease = acquire(tmp_path)
    calls = []
    def rotate(value):
        calls.append(value)
        return credentials("new-access", "new-refresh")
    monkeypatch.setattr(auth, "_refresh", rotate)
    client = broker_client(tmp_path)
    exchange = client._exchange
    commits = []
    def lose_first_receipt(operation, **values):
        result = exchange(operation, **values)
        if operation == "refresh_commit":
            commits.append(values)
            if len(commits) == 1:
                raise urllib.error.URLError("lost response")
        return result
    client._exchange = lose_first_receipt
    result = client.refresh_credentials(lease)
    assert result["auth"]["tokens"]["access_token"] == "new-access"
    assert len(calls) == 1 and len(commits) == 2 and commits[0] == commits[1]
    assert auth.auth_ready(tmp_path)


def test_lost_ticket_holder_quarantines_and_late_commit_is_cas_safe(tmp_path, monkeypatch):
    auth.install_auth(tmp_path, credentials())
    lease = acquire(tmp_path)
    ticket, status = auth.exchange(tmp_path, {"operation": "refresh_begin", "protocol": 2,
        "lease": lease["lease"], "generation": lease["generation"]})
    assert status == 200 and ticket["auth"] == credentials()
    now = time.time()
    monkeypatch.setattr(auth.time, "time", lambda: now + 121)
    assert auth.exchange(tmp_path, {"operation": "acquire", "protocol": 2})[1] == 503
    assert not auth.auth_ready(tmp_path)
    body = {"operation": "refresh_commit", "protocol": 2, "lease": lease["lease"],
            "ticket": ticket["ticket"], "auth": credentials("late-access", "late-refresh")}
    assert auth.exchange(tmp_path, {**body, "ticket": "forged"})[1] == 409
    assert auth.exchange(tmp_path, {**body, "auth": credentials(account="different-account")})[1] == 409
    assert auth.exchange(tmp_path, body)[1] == 200
    assert auth.auth_ready(tmp_path)
    # Administrative replacement must never be overwritten by a stale ticket.
    auth._save_auth(tmp_path, credentials("admin-access", "admin-refresh"))
    assert auth.exchange(tmp_path, body)[1] == 409
    assert json.loads(auth.auth_path(tmp_path).read_text())["tokens"]["access_token"] == "admin-access"


def test_broker_refresh_never_uses_vps_network(tmp_path, monkeypatch):
    auth.install_auth(tmp_path, credentials())
    lease = acquire(tmp_path)
    def forbidden(value):
        raise AssertionError("VPS must never contact upstream OAuth")
    monkeypatch.setattr(auth, "_refresh", forbidden)
    assert auth.exchange(tmp_path, {"operation": "refresh", "protocol": 2,
        "lease": lease["lease"], "generation": lease["generation"]})[1] == 503
    assert auth.exchange(tmp_path, {"operation": "refresh_begin", "protocol": 2,
        "lease": lease["lease"], "generation": lease["generation"]})[1] == 200
