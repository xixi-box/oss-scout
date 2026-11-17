"""Controller HTTP checks for the service without the retired web UI."""

import os
import sqlite3

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("OSSCOUT_DISABLE_AUTO_REPAIR", "1")

from dashboard.app import create_app
from dashboard.data import DataSource


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(tmp_path, password="pw-secret"))


def test_health_without_login_or_web_assets(client):
    assert client.get("/healthz").json() == {"ok": True}
    for path in ("/login", "/console-demo", "/prs", "/static/style.css"):
        assert client.get(path).status_code in (404, 405)


def test_admin_api_login_and_logout(client):
    path = "/api/console/models"
    assert client.get(path).status_code == 401
    assert client.post("/login", json={"password": "wrong"}).status_code == 401
    assert client.post("/login", json={"password": "pw-secret"}).json() == {"ok": True}
    assert client.get(path).status_code == 200
    assert client.post("/logout").json() == {"ok": True}
    assert client.get(path).status_code == 401


def test_manual_cloudflare_pull_reports_missing_configuration(client, monkeypatch):
    monkeypatch.delenv("CF_API_TOKEN", raising=False)
    assert client.post("/login", json={"password": "pw-secret"}).status_code == 200
    assert client.post("/api/cloudflare/pull").json() == {"error": "not configured"}


def test_machine_routes_keep_independent_authentication(client, monkeypatch):
    monkeypatch.setenv("CF_INGEST_TOKEN", "worker-token")
    monkeypatch.setenv("FEISHU_VERIFICATION_TOKEN", "feishu-token")
    assert client.post("/api/ingest/issues", json={"issues": []}).status_code == 403
    headers = {"X-Ingest-Token": "worker-token"}
    assert client.post("/api/ingest/issues", headers=headers, json={"issues": []}).status_code == 200
    assert client.post("/api/ingest/pr-comments", headers=headers, json={"events": []}).status_code == 404
    assert client.post("/login", json={"password": "pw-secret"}).status_code == 200
    assert client.post("/api/pr/reply/fix-result", headers=headers, json={}).status_code == 404
    assert client.post("/feishu/callback", json={"event": {"action": {"value": {"do": "approve_pr_followup"}}}}).status_code == 404
    reply = client.post("/feishu/callback", json={"type": "url_verification", "challenge": "test"})
    assert reply.status_code == 404


def test_controller_opens_sqlite_read_only(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    db = sqlite3.connect(state / "scout_v2.sqlite3")
    db.execute("CREATE TABLE sample (value TEXT)")
    db.close()
    conn = DataSource(tmp_path)._open_ro()
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("INSERT INTO sample VALUES ('value')")
    finally:
        conn.close()


def test_inbox_lock_wait_does_not_block_http_event_loop(tmp_path, monkeypatch):
    import asyncio
    import threading
    import time
    import httpx
    from scout_v2.state_store import IssueInbox

    monkeypatch.setenv("CF_INGEST_TOKEN", "worker-token")
    release = threading.Event()
    entered = threading.Event()

    def blocked_put(self, issues):
        entered.set()
        assert release.wait(3)
        return len(issues)

    monkeypatch.setattr(IssueInbox, "put", blocked_put)
    app = create_app(tmp_path, password="pw-secret")

    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            task = asyncio.create_task(client.post("/api/ingest/issues",
                headers={"X-Ingest-Token": "worker-token"},
                json={"issues": [{"repo": "a/b", "number": 1}]}))
            watchdog = threading.Timer(1, release.set)
            watchdog.start()
            try:
                started = time.monotonic()
                assert await asyncio.to_thread(entered.wait, 2)
                assert (await client.get("/healthz")).json() == {"ok": True}
                assert time.monotonic() - started < 0.6
            finally:
                release.set()
                watchdog.cancel()
                response = await task
            assert response.status_code == 200
            assert response.json()["stored"] == 1

    asyncio.run(check())


def test_inbox_busy_returns_retryable_status_and_closes_connection(tmp_path, monkeypatch):
    from scout_v2.state_store import IssueInbox

    monkeypatch.setenv("CF_INGEST_TOKEN", "worker-token")
    closed = []
    real_close = IssueInbox.close

    def busy(self, issues):
        raise sqlite3.OperationalError("database is locked")

    def close(self):
        closed.append(True)
        real_close(self)

    monkeypatch.setattr(IssueInbox, "put", busy)
    monkeypatch.setattr(IssueInbox, "close", close)
    with TestClient(create_app(tmp_path, password="pw-secret")) as client:
        response = client.post("/api/ingest/issues", headers={"X-Ingest-Token": "worker-token"},
                               json={"issues": [{"repo": "a/b", "number": 1}]})
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    assert closed == [True]
