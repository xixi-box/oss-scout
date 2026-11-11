import json
import subprocess
from pathlib import Path

import pytest

from scout_v2 import codex_auth as auth
from scout_v2.console_models import repair_settings, repair_model_selector, write_models
from scout_v2.workers.codex_client import CodexClient


def credentials(token="access"):
    return {"tokens": {"access_token": token, "refresh_token": "refresh", "account_id": "test-account"}}


def test_subscription_auth_is_exclusive_and_refresh_is_saved(tmp_path, monkeypatch):
    assert not auth.auth_ready(tmp_path)
    assert auth.exchange(tmp_path, {"operation": "acquire"})[1] == 401
    auth.install_auth(tmp_path, credentials())
    first, status = auth.exchange(tmp_path, {"operation": "acquire"})
    assert status == 200 and first["auth"] == credentials()
    assert auth.exchange(tmp_path, {"operation": "acquire"})[1] == 409
    assert auth.exchange(tmp_path, {"operation": "sync", "lease": "wrong", "auth": credentials("wrong")})[1] == 409
    assert auth.exchange(tmp_path, {"operation": "sync", "lease": first["lease"],
                                   "auth": credentials("refreshed")})[1] == 200
    assert auth.exchange(tmp_path, {"operation": "release", "lease": first["lease"]})[1] == 200
    second, _ = auth.exchange(tmp_path, {"operation": "acquire"})
    assert second["auth"] == credentials("refreshed")
    monkeypatch.setattr(auth.time, "time", lambda: 10**12)
    third, _ = auth.exchange(tmp_path, {"operation": "acquire"})
    assert third["lease"] != second["lease"]
    assert auth.exchange(tmp_path, {"operation": "release", "lease": second["lease"]})[1] == 409
    with pytest.raises(ValueError):
        auth.install_auth(tmp_path, {"OPENAI_API_KEY": "api-key"})


def test_switch_preserves_separate_models_and_old_jobs_default_to_opencode(tmp_path):
    from scout_v2.schemas import Job

    write_models(tmp_path, {"repair_model": "opencode-go/test", "opencode_variant": "high",
                           "codex_model": "test-codex", "codex_variant": "xhigh"})
    assert repair_settings(tmp_path)[0] == "opencode"
    assert repair_model_selector(tmp_path) == "opencode-go/test#high"
    write_models(tmp_path, {"repair_agent": "codex", "repair_model": "opencode-go/test",
                           "codex_model": "test-codex", "codex_variant": "xhigh"})
    assert repair_model_selector(tmp_path) == "test-codex#xhigh"
    assert Job.from_dict({}).repair_agent == "opencode"
    assert Job.from_dict({"repair_agent": "codex"}).to_dict()["repair_agent"] == "codex"


def test_codex_client_updates_login_and_has_no_api_key_in_child_env(tmp_path, monkeypatch):
    from scout_v2.workers import codex_client as module

    calls, command = [], []
    monkeypatch.setenv("OPENCODE_API_KEY", "private")
    monkeypatch.setenv("OPENAI_API_KEY", "private-api")
    monkeypatch.setenv("CF_INGEST_TOKEN", "private-broker")
    client = CodexClient(timeout_seconds=100, workdir=tmp_path)

    def exchange(operation, **values):
        calls.append((operation, values))
        return {"lease": "lease", "auth": credentials()} if operation == "acquire" else {"ok": True}

    class Process:
        def __init__(self, args, **kwargs):
            command.extend(args)
            self.home = Path(kwargs["env"]["CODEX_HOME"])
            assert not any(key in kwargs["env"] for key in ("OPENCODE_API_KEY", "OPENAI_API_KEY", "CF_INGEST_TOKEN"))
            assert kwargs["stdin"].read() == "fix this issue"
            self.completed = False
            self.waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                (self.home / "auth.json").write_text(json.dumps(credentials("refreshed")))
                raise subprocess.TimeoutExpired("codex", timeout)
            Path(command[command.index("-o") + 1]).write_text("fixed with regression test")
            self.completed = True
            return 0

        def poll(self):
            return 0 if self.completed else None

    monkeypatch.setattr(client, "_exchange", exchange)
    monkeypatch.setattr(module.subprocess, "Popen", Process)
    ok, result = client.run("test-model#high", "fix this issue")
    assert ok and result == "fixed with regression test"
    assert command[command.index("-m") + 1] == "test-model"
    assert 'model_reasoning_effort="high"' in command
    assert [operation for operation, _ in calls] == ["acquire", "sync", "release"]
    assert calls[-1][1]["auth"] == credentials("refreshed")


def test_codex_start_failure_releases_lease_without_exposing_credentials(tmp_path, monkeypatch):
    from scout_v2.workers import codex_client as module

    client = CodexClient(timeout_seconds=100, workdir=tmp_path)
    calls = []

    def exchange(operation, **values):
        calls.append(operation)
        return {"lease": "lease", "auth": credentials()}

    monkeypatch.setattr(client, "_exchange", exchange)
    monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError()))
    ok, result = client.run("", "fix")
    assert not ok and "access" not in result
    assert calls == ["acquire", "release"]


def test_broker_http_requires_ingest_token_and_disables_cache(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from dashboard.app import create_app
    from dashboard import data

    monkeypatch.setenv("OSSCOUT_DISABLE_AUTO_REPAIR", "1")
    monkeypatch.setattr(data, "cloudflare_cfg", lambda _: {"ingest_token": "test"})
    auth.install_auth(tmp_path, credentials())
    app = create_app(root=tmp_path)
    with TestClient(app) as client:
        assert client.post("/api/ingest/codex-auth", json={"operation": "acquire"}).status_code == 403
        response = client.post("/api/ingest/codex-auth", json={"operation": "acquire"},
                               headers={"X-Ingest-Token": "test"})
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
