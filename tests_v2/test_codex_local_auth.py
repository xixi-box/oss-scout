import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scout_v2.workers import codex_client as module


def setup_local(tmp_path, monkeypatch, script, *, timeout=5):
    work = tmp_path / "repo"
    work.mkdir()
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    auth = private / "auth.json"
    auth.write_text(json.dumps({"tokens": {"account_id": "owner-account",
        "access_token": "initial-access", "refresh_token": "initial-refresh"}}))
    auth.chmod(0o600)
    monkeypatch.setenv("OSSCOUT_CODEX_AUTH_MODE", "github_secrets")
    monkeypatch.setenv("OSSCOUT_CODEX_AUTH_FILE", str(auth))
    monkeypatch.setenv("GH_TOKEN", "private-writer-token")
    monkeypatch.setenv("CODEX_REFRESH_TOKEN_URL_OVERRIDE", "https://old-broker.invalid")
    cli = tmp_path / "cli.py"
    cli.write_text("import json,os,sys,time\nfrom pathlib import Path\n" + script)
    popen = subprocess.Popen
    monkeypatch.setattr(module.subprocess, "Popen", lambda args, **kw:
        popen([sys.executable, str(cli), *args[1:]], **kw))
    monkeypatch.setattr(module.CodexClient, "_exchange", lambda *a, **kw:
                        pytest.fail("local auth must never contact broker"))
    client = module.CodexClient(timeout_seconds=timeout, workdir=work,
                               diagnostics_dir=tmp_path / "diagnostics")
    return client, auth


REFRESH = '''
assert not any(k in os.environ for k in ("OSSCOUT_CODEX_AUTH_FILE", "OSSCOUT_CODEX_AUTH_MODE", "GH_TOKEN", "CODEX_REFRESH_TOKEN_URL_OVERRIDE"))
auth = Path(os.environ["CODEX_HOME"]) / "auth.json"
value = json.loads(auth.read_text())
value["tokens"]["access_token"] = "rotated-access"
value["tokens"]["refresh_token"] += "-rotated"
auth.write_text(json.dumps(value))
print(json.dumps({"type":"error", "message":value["tokens"]["refresh_token"]}), flush=True)
'''


def test_repair_and_read_only_review_reuse_refresh_without_broker(tmp_path, monkeypatch, capsys):
    client, auth = setup_local(tmp_path, monkeypatch, REFRESH + '''
Path(sys.argv[sys.argv.index("-o") + 1]).write_text("done")
''')
    assert client.run("gpt-6.1-sol#medium", "repair") == (True, "done")
    client.read_only = True
    assert client.run("gpt-6.1-sol#medium", "review") == (True, "done")
    value = json.loads(auth.read_text())
    assert value["tokens"]["refresh_token"] == "initial-refresh-rotated-rotated"
    visible = (tmp_path / "diagnostics/codex-events.jsonl").read_text() + capsys.readouterr().out
    assert "rotated-access" not in visible and "initial-refresh" not in visible
    assert "private-writer-token" not in visible
    assert not list((tmp_path / "diagnostics").rglob("auth.json"))
    assert not (auth.parent / "writeback-blocked").exists()


@pytest.mark.parametrize("ending,timeout", [("raise SystemExit(7)", 5), ("time.sleep(60)", .7)])
def test_failure_and_timeout_preserve_refreshed_credentials(tmp_path, monkeypatch, ending, timeout):
    client, auth = setup_local(tmp_path, monkeypatch, REFRESH + ending, timeout=timeout)
    ok, detail = client.run("model#medium", "repair")
    assert not ok
    assert "initial-refresh-rotated" not in detail
    assert json.loads(auth.read_text())["tokens"]["refresh_token"] == "initial-refresh-rotated"
    assert not (auth.parent / "writeback-blocked").exists()


@pytest.mark.parametrize("invalid", ["missing", "broken", "inside-work", "public"])
def test_invalid_or_exposed_auth_is_rejected_before_cli(tmp_path, monkeypatch, invalid):
    client, auth = setup_local(tmp_path, monkeypatch, "raise AssertionError('must not start')")
    if invalid == "missing":
        auth.unlink()
    elif invalid == "broken":
        auth.write_text("bad json; private-secret")
    elif invalid == "inside-work":
        client.workdir = auth.parent
    elif os.name == "nt":
        pytest.skip("POSIX file permissions")
    else:
        auth.chmod(0o644)
    ok, detail = client.run("model", "repair")
    assert not ok and "private-secret" not in detail
    events = (tmp_path / "diagnostics/codex-events.jsonl").read_text()
    assert '"type": "agent.started"' not in events


def test_external_update_is_not_overwritten_and_rotated_auth_is_saved_for_recovery(tmp_path, monkeypatch):
    client, auth = setup_local(tmp_path, monkeypatch, REFRESH + f'''
shared = Path({str(tmp_path / "private/auth.json")!r})
external = json.loads(shared.read_text())
external["tokens"]["refresh_token"] = "external-refresh"
shared.write_text(json.dumps(external))
Path(sys.argv[sys.argv.index("-o") + 1]).write_text("done")
''')
    ok, detail = client.run("model", "repair")
    assert not ok and "recovery required" in detail
    assert json.loads(auth.read_text())["tokens"]["refresh_token"] == "external-refresh"
    recovery = json.loads(auth.with_name("recovery-auth.json").read_text())
    assert recovery["tokens"]["refresh_token"] == "initial-refresh-rotated"
    assert auth.with_name("writeback-blocked").exists()
    with pytest.raises(RuntimeError, match="recovery before reuse"):
        module._LocalAuth(client.workdir, client.diagnostics_dir)


def test_different_account_is_not_persisted(tmp_path, monkeypatch):
    client, auth = setup_local(tmp_path, monkeypatch, REFRESH + '''
value["tokens"]["account_id"] = "other-account"
auth.write_text(json.dumps(value))
Path(sys.argv[sys.argv.index("-o") + 1]).write_text("done")
''')
    assert not client.run("model", "repair")[0]
    assert json.loads(auth.read_text())["tokens"]["account_id"] == "owner-account"
    assert auth.with_name("writeback-blocked").exists()


def test_local_auth_lock_excludes_another_process(tmp_path, monkeypatch):
    popen = subprocess.Popen
    _, auth = setup_local(tmp_path, monkeypatch, "")
    monkeypatch.setattr(module.subprocess, "Popen", popen)
    lock = module._LocalAuth(tmp_path / "repo", None)
    try:
        code = "from pathlib import Path; from scout_v2.workers.codex_client import _LocalAuth; _LocalAuth(Path.cwd(), None)"
        child = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert child.returncode != 0 and "busy" in child.stderr
    finally:
        lock.close()
    second = module._LocalAuth(tmp_path / "repo", None)
    second.close()
    assert auth.exists()


def test_local_heartbeat_is_diagnostic_and_has_no_lease_deadline(tmp_path, monkeypatch):
    client, auth = setup_local(tmp_path, monkeypatch, "", timeout=2400)
    clock = [0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module, "_prepare_process_cleanup", lambda: (set(), False))
    monkeypatch.setattr(module, "_stop_process_tree", lambda *args: [])
    monkeypatch.setattr(module, "_restore_subreaper", lambda *args: None)

    class Process:
        def __init__(self, args, **kwargs):
            self.output = Path(args[args.index("-o") + 1])

        def wait(self, timeout):
            clock[0] += timeout
            if clock[0] < 200:
                raise subprocess.TimeoutExpired("codex", timeout)
            self.output.write_text("done")
            return 0

    monkeypatch.setattr(module.subprocess, "Popen", Process)
    assert client.run("model", "repair") == (True, "done")
    rows = [json.loads(line) for line in (tmp_path / "diagnostics/codex-events.jsonl").read_text().splitlines()]
    assert sum(row["type"] == "agent.heartbeat" for row in rows) >= 9
    assert auth.exists()


@pytest.mark.skipif(os.name == "nt", reason="symlinks need Windows developer privileges")
@pytest.mark.parametrize("replace", ["shared", "cli", "directory"])
def test_symlink_replacement_is_rejected(tmp_path, monkeypatch, replace):
    client, auth = setup_local(tmp_path, monkeypatch, REFRESH)
    other = tmp_path / "unrelated"
    other.write_text("private-unrelated-secret")
    if replace == "shared":
        auth.unlink()
        auth.symlink_to(other)
    else:
        cli = tmp_path / "cli.py"
        if replace == "cli":
            cli.write_text("import os\nfrom pathlib import Path\nauth=Path(os.environ['CODEX_HOME'])/'auth.json'\nauth.unlink()\nauth.symlink_to(" + repr(str(other)) + ")")
        else:
            cli.write_text("import os\nfrom pathlib import Path\np=Path(" + repr(str(auth.parent)) + ")\np.rename(p.with_name('moved'))\np.symlink_to(" + repr(str(other.parent)) + ",target_is_directory=True)")
    ok, detail = client.run("model", "repair")
    assert not ok and "private-unrelated-secret" not in detail
    assert other.read_text() == "private-unrelated-secret"
