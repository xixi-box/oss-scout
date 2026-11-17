"""Trusted credential lifecycle: no old family reuse after uncertain writes."""
import importlib.util
import json
import os
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("secret_session", ROOT / "scripts/codex_secret_session.py")
session = importlib.util.module_from_spec(spec)
spec.loader.exec_module(session)


@pytest.fixture
def lifecycle(tmp_path, monkeypatch):
    auth = {"tokens": {"access_token": "access", "refresh_token": "refresh", "account_id": "owner"}}
    state = {"state": "ready", "generation": session.digest(auth)}
    writes = []
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output"))
    monkeypatch.setenv("CODEX_AUTH_JSON", json.dumps(auth))
    monkeypatch.setattr(session, "read_state", lambda: state.copy())
    def update(value):
        state.clear()
        state.update(value)
    monkeypatch.setattr(session, "set_state", update)
    monkeypatch.setattr(session, "gh", lambda *args, data=None: writes.append((args, data)))
    return auth, state, writes


def test_cli_rotation_persists_before_release(lifecycle):
    auth, state, writes = lifecycle
    session.restore()
    assert state["state"] == "in_use"
    auth["tokens"]["refresh_token"] = "rotated"
    path = session.directory() / "auth.json"
    path.write_text(json.dumps(auth))
    session.persist()
    assert json.loads(writes[0][1])["tokens"]["refresh_token"] == "rotated"
    assert state == {"state": "ready", "generation": session.digest(auth)}
    session.cleanup()
    assert not session.directory().exists()


def test_unchanged_auth_releases_without_rewriting_secret(lifecycle):
    _, state, writes = lifecycle
    session.restore()
    session.persist()
    assert not writes and state["state"] == "ready"


def test_write_failure_keeps_guard_busy(lifecycle, monkeypatch):
    auth, state, _ = lifecycle
    session.restore()
    auth["tokens"]["refresh_token"] = "rotated"
    (session.directory() / "auth.json").write_text(json.dumps(auth))
    def fail(*a, **k):
        raise RuntimeError("network unavailable")
    monkeypatch.setattr(session, "gh", fail)
    with pytest.raises(RuntimeError):
        session.persist()
    assert state["state"] == "in_use"


@pytest.mark.parametrize("change", ["busy", "stale", "branch"])
def test_restore_never_falls_back(lifecycle, monkeypatch, change):
    _, state, _ = lifecycle
    if change == "busy":
        state["state"] = "in_use"
    elif change == "stale":
        state["generation"] = "other"
    else:
        monkeypatch.setenv("GITHUB_REF", "refs/heads/other")
    with pytest.raises(ValueError):
        session.restore()
    assert not session.directory().exists()


@pytest.mark.parametrize("change", ["owner", "account", "local_identity", "blocked"])
def test_persist_protects_remote_identity_and_ownership(lifecycle, change):
    auth, state, writes = lifecycle
    session.restore()
    if change == "owner":
        state["nonce"] = "changed"
    elif change in ("account", "local_identity"):
        auth["tokens"]["account_id"] = "other"
        (session.directory() / "auth.json").write_text(json.dumps(auth))
        if change == "local_identity":
            path = session.directory() / "session.json"
            metadata = json.loads(path.read_text())
            metadata["identity"] = session.identity(auth)
            path.write_text(json.dumps(metadata))
    else:
        (session.directory() / "writeback-blocked").touch()
    with pytest.raises(ValueError):
        session.persist()
    assert not writes and state["state"] == "in_use"


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink and permissions")
def test_cleanup_does_not_follow_replaced_directory(lifecycle, tmp_path):
    session.restore()
    session.cleanup()
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "auth.json").write_text("unrelated")
    session.directory().symlink_to(victim, target_is_directory=True)
    with pytest.raises(ValueError):
        session.cleanup()
    assert (victim / "auth.json").read_text() == "unrelated"


def test_all_credential_workflows_share_session_and_always_persist():
    for filename, job in (("oss-scout-repair.yml", "repair"), ("codex-login.yml", "check"),
                          ("codex-concurrency-check.yml", "check")):
        workflow = yaml.safe_load((ROOT / ".github/workflows" / filename).read_text(encoding="utf-8"))
        config = workflow["jobs"][job]
        assert config["environment"] == "codex-auth"
        assert config["concurrency"] == {"group": "codex-account-session", "cancel-in-progress": False}
        steps = config["steps"]
        persist = next(s for s in steps if "persist" in s.get("run", "") and "codex_secret_session.py" in s["run"])
        assert persist["if"] == "always() && steps.codex_auth.outputs.ready == 'true'"
        for step in steps:
            if step.get("env", {}).get("OSSCOUT_CODEX_AUTH_MODE") == "github_secrets":
                assert not {"GH_TOKEN", "GITHUB_TOKEN", "CF_INGEST_TOKEN", "DASHBOARD_URL", "CODEX_AUTH_JSON"} & set(step["env"])
        if job == "repair":
            publish = next(s for s in steps if s.get("id") == "publish")
            assert steps.index(persist) < steps.index(publish)
