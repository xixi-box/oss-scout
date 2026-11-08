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
    assert client.post("/api/pr/reply/fix-result", headers=headers, json={}).status_code == 400
    assert client.post("/feishu/callback", json={}).status_code == 403
    reply = client.post("/feishu/callback", json={"type": "url_verification", "challenge": "test"})
    assert reply.json() == {"challenge": "test"}


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
