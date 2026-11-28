"""Both repair CLIs own tests; publication receives the complete bound patch."""
import hashlib
import json
import subprocess

import pytest

from scout_v2.workers import repair
from test_p5_repair import make_repo, job


@pytest.mark.parametrize("agent", ["opencode", "codex"])
def test_complete_delivery_is_not_replayed_or_split(tmp_path, agent):
    repo, sha = make_repo(tmp_path)
    task = job(sha)
    task.repair_agent = agent
    marker = tmp_path / "must-not-run"
    body = "fn value() -> i32 { 1 }\n#[cfg(test)]\nmod tests { /* inline regression */ }\n"

    class Client:
        def run(self, *_):
            (work / "aml.rs").write_text(body)
            (work / "fixture.bin").write_bytes(b"\x00binary-fixture\xff")
            (work / repair.RESULT_FILE).write_text(json.dumps({
                "test_file": "aml.rs", "test_cmd": f"touch {marker}",
                "base_failure": ["\x1b[31massertion failed\x1b[0m"],
                "patched_success": ["wrapper: 0 collected, worker: passed"],
                "test_report": {"path": "report.xml"},
                "self_check": ["checked real entrypoint and sibling paths"],
            }))
            (work / "report.xml").write_text("<testsuite/>")
            return True, "Agent completed tests and self-check"

    def producer(j, directory, context):
        nonlocal work
        work = directory
        return repair.opencode_patch_producer(j, work, context, client=Client())

    work = None
    out = tmp_path / "out"
    result = repair.run_repair(task, repo, out, patch_producer=producer)
    assert result.outcome == "succeeded", result.unverified_items
    assert result.verification_mode == "agent"
    assert not result.reproduced_on_base and not result.fixed_on_patch
    assert not marker.exists()
    patch = (out / "change.patch").read_bytes()
    assert b"GIT binary patch" in patch and b"inline regression" in patch
    assert b"report.xml" not in patch and b"OSSCOUT_RESULT" not in patch
    subprocess.run(["git", "apply", str(out / "change.patch")], cwd=repo, check=True)
    assert (repo / "fixture.bin").read_bytes() == b"\x00binary-fixture\xff"
    assert (repo / "aml.rs").read_text() == body
    report = json.loads((out / "test-report.json").read_text())
    assert report["delivery_patch_sha256"] == hashlib.sha256(patch).hexdigest()
    assert report["agent_report"]["self_check"]
    assert "base_returncode" not in report


@pytest.mark.parametrize("agent", ["opencode", "codex"])
def test_cli_failure_cannot_publish_a_partial_patch(tmp_path, agent):
    repo, sha = make_repo(tmp_path)
    task = job(sha)
    task.repair_agent = agent
    result = repair.run_repair(task, repo, tmp_path / "out", patch_producer=lambda *_: {
        "patch": "partial patch", "agent_failure": "timeout", "agent_report": {"publication_ready": True}})
    assert result.outcome == "failed"
    assert not (tmp_path / "out/change.patch").exists()


@pytest.mark.parametrize("agent", ["opencode", "codex"])
def test_agent_switching_base_cannot_deliver_a_patch(tmp_path, agent):
    repo, sha = make_repo(tmp_path)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit",
                    "--allow-empty", "-qm", "feature branch baseline"], cwd=repo, check=True)
    other = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    task = job(sha)
    task.repair_agent = agent

    def producer(j, work, context):
        class Client:
            def run(self, *_):
                subprocess.run(["git", "checkout", "--detach", other], cwd=work, check=True)
                (work / "feature.txt").write_text("change on the wrong base\n")
                return True, "done"

        return repair.opencode_patch_producer(j, work, context, client=Client())

    out = tmp_path / "out"
    result = repair.run_repair(task, repo, out, patch_producer=producer)
    assert result.outcome == "failed"
    assert result.unverified_items == [f"agent_worktree_base_changed: expected {sha}, found {other}"]
    assert not (out / "change.patch").exists()


def test_opencode_full_timeout_output_is_retained_and_redacted(tmp_path, monkeypatch):
    from scout_v2.workers import opencode_client
    monkeypatch.setenv("OPENCODE_API_KEY", "private-model-secret")
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 1,
            output=b"first command\nprivate-model-secret\nlast command", stderr=b"build still running")
    monkeypatch.setattr(opencode_client.subprocess, "run", timeout)
    client = opencode_client.OpenCodeClient(workdir=tmp_path, diagnostics_dir=tmp_path / "logs")
    ok, _ = client.run("model", "task")
    assert not ok
    record = json.loads((tmp_path / "logs/opencode-commands.jsonl").read_text())
    assert record["timed_out"] and record["returncode"] is None
    stdout = (tmp_path / "logs" / record["stdout_file"]).read_text()
    assert "first command" in stdout and "last command" in stdout
    assert "private-model-secret" not in stdout
