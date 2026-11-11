import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scout_v2.workers import codex_client as module
from scout_v2.workers.codex_client import CodexClient


def fake_cli(tmp_path, monkeypatch, script, *, timeout=5):
    cli = tmp_path / "fake_codex.py"
    cli.write_text(script, encoding="utf-8")
    real_popen = subprocess.Popen
    monkeypatch.setattr(module.subprocess, "Popen", lambda args, **kw:
                        real_popen([sys.executable, str(cli)] if "--ignore-user-config" in args
                                   else args, **kw))
    client = CodexClient(timeout_seconds=timeout, workdir=tmp_path,
                         diagnostics_dir=tmp_path / "artifacts")
    calls = []

    def exchange(operation, **values):
        calls.append(operation)
        return {"lease": "private-lease", "auth": {"tokens": {
            "access_token": "private-access-token", "refresh_token": "private-refresh-token"}}}

    monkeypatch.setattr(client, "_exchange", exchange)
    return client, calls


def test_failed_cli_retains_commands_output_exit_codes_and_redacts_credentials(tmp_path, monkeypatch, capsys):
    script = '''import json, time
print(json.dumps({"type":"item.started","item":{"id":"cmd1","type":"command_execution","command":"pytest tests/test_parser.py"}}),flush=True)
time.sleep(.03)
print(json.dumps({"type":"item.completed","item":{"id":"cmd1","type":"command_execution","command":"pytest tests/test_parser.py","exit_code":2,"aggregated_output":"missing dependency; private-access-token"}}),flush=True)
print(json.dumps({"type":"error","message":"setup failed; private-refresh-token; secret-provider-key"}),flush=True)
raise SystemExit(7)
'''
    monkeypatch.setenv("OPENCODE_API_KEY", "secret-provider-key")
    client, calls = fake_cli(tmp_path, monkeypatch, script)
    ok, detail = client.run("gpt-6.1-sol#medium", "fix")
    assert not ok and "status 7" in detail and "setup failed" in detail
    assert calls == ["acquire", "release"]
    text = (tmp_path / "artifacts/codex-events.jsonl").read_text(encoding="utf-8")
    rows = [json.loads(line) for line in text.splitlines()]
    completed = next(row["event"] for row in rows if row.get("event", {}).get("type") == "item.completed")
    assert completed["item"]["exit_code"] == 2
    assert "missing dependency" in completed["item"]["aggregated_output"]
    assert completed["command_elapsed_seconds"] >= .01
    assert rows[0]["model"] == "gpt-6.1-sol" and rows[0]["reasoning_effort"] == "medium"
    assert rows[-1]["returncode"] == 7 and rows[-1]["ok"] is False
    visible = text + detail + capsys.readouterr().out
    for secret in ("private-access-token", "private-refresh-token", "secret-provider-key", "private-lease"):
        assert secret not in visible
    assert not list(tmp_path.glob("artifacts/**/auth.json"))


def test_timeout_retains_partial_output_and_releases_subscription(tmp_path, monkeypatch):
    client, calls = fake_cli(tmp_path, monkeypatch, '''import json,time
print(json.dumps({"type":"item.started","item":{"id":"build","type":"command_execution","command":"ninja target"}}),flush=True)
time.sleep(60)
''', timeout=.8)
    ok, detail = client.run("test#medium", "fix")
    assert not ok and "timed out" in detail and "ninja target" in detail
    assert calls[-1] == "release"
    rows = [json.loads(line) for line in (tmp_path / "artifacts/codex-events.jsonl").read_text().splitlines()]
    assert rows[-1]["type"] == "agent.finished" and rows[-1]["ok"] is False
    assert not rows[-1]["cleanup_errors"]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux hosted runner process adoption")
def test_detached_build_daemon_is_cleaned_even_after_cli_exits(tmp_path, monkeypatch):
    pidfile = tmp_path / "daemon.pid"
    child = f"import os,time,pathlib; pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid())); time.sleep(60)"
    script = f'''import subprocess,sys,time,pathlib
subprocess.Popen([sys.executable,'-c',{child!r}],start_new_session=True)
for _ in range(100):
    if pathlib.Path({str(pidfile)!r}).exists(): break
    time.sleep(.01)
raise SystemExit(1)
'''
    client, _ = fake_cli(tmp_path, monkeypatch, script)
    ok, _ = client.run("test", "fix")
    assert not ok
    pid = int(pidfile.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_workspace_cleanup_failure_does_not_replace_the_repair_failure(tmp_path, monkeypatch):
    from scout_v2.schemas import Job
    from scout_v2.workers import repair

    spaces = []
    original = repair._RepairWorkspace

    def workspace(out):
        space = original(out)
        spaces.append(space)
        return space

    try:
        with monkeypatch.context() as patch:
            patch.setattr(repair, "_RepairWorkspace", workspace)
            patch.setattr(original, "_rmtree", classmethod(lambda *a, **kw:
                          (_ for _ in ()).throw(OSError("directory not empty"))))
            patch.setattr(repair, "_checkout_repair_tree", lambda repo, work, sha: work.mkdir())
            result = repair.run_repair(Job(job_id="job-cleanup", base_sha="abc"),
                tmp_path, tmp_path / "artifacts", patch_producer=lambda *a:
                (_ for _ in ()).throw(RuntimeError("agent timed out")))
        assert result.outcome == "failed" and result.unverified_items == ["agent timed out"]
        assert "result retained" in (tmp_path / "artifacts/cleanup.log").read_text()
    finally:
        for space in spaces:
            space.cleanup()
