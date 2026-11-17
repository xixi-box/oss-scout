import base64
import json
from pathlib import Path
import subprocess

import pytest

from scripts import verify_codex_session as check


def test_native_check_expires_jwt_and_accepts_unchanged_refresh_token(tmp_path, monkeypatch):
    claims = base64.urlsafe_b64encode(json.dumps({"exp": 9999999999}).encode()).decode().rstrip("=")
    path = tmp_path / "auth.json"
    path.write_text(json.dumps({"tokens": {"access_token": "header." + claims + ".signature",
                                          "refresh_token": "unchanged-refresh"}}))
    monkeypatch.setenv("OSSCOUT_CODEX_AUTH_MODE", "github_secrets")
    monkeypatch.setenv("OSSCOUT_CODEX_AUTH_FILE", str(path))
    monkeypatch.setenv("CHECK_REFRESH", "true")
    modes = []
    class Client:
        def __init__(self, *, workdir, read_only, **kw):
            assert subprocess.run(["git", "rev-parse", "--git-dir"], cwd=workdir, capture_output=True).returncode == 0
            modes.append(read_only)
        def run(self, model, prompt):
            value = json.loads(path.read_text())
            if len(modes) == 1:
                assert json.loads(base64.urlsafe_b64decode(value["tokens"]["access_token"].split(".")[1] + "=="))["exp"] == 0
                value["tokens"]["access_token"] = "fresh-access"
                value["last_refresh"] = "2026-11-17T00:00:00Z"
                path.write_text(json.dumps(value))
            else:
                assert value["tokens"]["access_token"] == "fresh-access"
            return True, "OSS_SCOUT_OK"
    monkeypatch.setattr(check, "CodexClient", Client)
    check.main()
    assert modes == [False, True]
    assert json.loads(path.read_text())["tokens"]["refresh_token"] == "unchanged-refresh"


def test_failed_refresh_probe_blocks_artificial_credential_writeback(tmp_path, monkeypatch):
    path = tmp_path / "auth.json"
    path.write_text(json.dumps({"tokens": {"access_token": "opaque-original", "refresh_token": "real-refresh"}}))
    monkeypatch.setenv("OSSCOUT_CODEX_AUTH_MODE", "github_secrets")
    monkeypatch.setenv("OSSCOUT_CODEX_AUTH_FILE", str(path))
    monkeypatch.setenv("CHECK_REFRESH", "true")
    class Client:
        def __init__(self, **kw):
            pass
        def run(self, *a):
            return False, "temporary upstream error"
    monkeypatch.setattr(check, "CodexClient", Client)
    with pytest.raises(RuntimeError, match="temporary upstream error"):
        check.main()
    assert path.with_name("writeback-blocked").exists()
