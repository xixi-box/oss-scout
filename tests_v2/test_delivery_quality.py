"""Exercise the shared agent -> independent verification -> delivered patch contract."""
import hashlib
import json
import subprocess
import sys

import pytest

from scout_v2.schemas import Job
from scout_v2.workers import publish_remote, repair
from scout_v2.workers.quality_gate import check_delivery_scope
from test_p5_repair import make_repo


@pytest.fixture(params=["opencode", "codex"])
def delivered(request, tmp_path):
    repo, sha = make_repo(tmp_path)
    job = Job(mode="repair", repo="pypa/pipx", base_sha=sha,
              idempotency_key=f"repair:{sha}", repair_agent=request.param)
    command = [sys.executable.replace(chr(92), "/"), "-m", "pytest", "test_regression.py", "-v"]

    class Client:
        def run(self, model, prompt):
            (self.workdir / "calc.py").write_text("def div(a, b):\n    return a / b\n", encoding="utf-8")
            (self.workdir / "test_regression.py").write_text(
                "from calc import div\ndef test_fractional_division():\n    assert div(7, 2) == 3.5\n",
                encoding="utf-8")
            junk = self.workdir / "UNKNOWN.egg-info" / "PKG-INFO"
            junk.parent.mkdir()
            junk.write_text("generated metadata", encoding="utf-8")
            (self.workdir / repair.RESULT_FILE).write_text(json.dumps({
                "test_file": "test_regression.py", "test_cmd": command,
                "setup_steps": [], "base_failure": ["test_fractional_division", "assert 3 == 3.5"],
                "patched_success": ["passed"],
                "root_cause": "Floor division truncates fractional results.",
            }), encoding="utf-8")
            return True, ""

    def producer(j, work, context):
        client = Client()
        client.workdir = work
        return repair.opencode_patch_producer(j, work, context, client=client, model="fixture")

    out = tmp_path / "out"
    result = repair.run_repair(job, repo, out, patch_producer=producer)
    assert result.outcome == "succeeded", result.unverified_items
    (out / "result.json").write_text(json.dumps(result.to_dict()), encoding="utf-8")
    payload = job.to_dict()
    payload["publish"] = {"upstream": job.repo, "issue_number": 123,
                          "title": "fix: fractional division", "body": "Fixes #123\nAI assistance: OpenCode"}
    return repo, out, payload, command


def test_exact_verified_patch_can_be_delivered_by_either_agent(delivered, monkeypatch, tmp_path):
    repo, out, payload, command = delivered
    patch = (out / "change.patch").read_text(encoding="utf-8")
    assert "calc.py" in patch and "test_regression.py" in patch
    assert "egg-info" not in patch
    report = json.loads((out / "test-report.json").read_text(encoding="utf-8"))
    assert report["base_returncode"] == 1 and report["patched_returncode"] == 0

    def publish(repo_dir, patch_file, config):
        checkout = tmp_path / "recipient"
        subprocess.run(["git", "clone", "-q", str(repo), str(checkout)], check=True)
        subprocess.run(["git", "apply", str(patch_file.resolve())], cwd=checkout, check=True)
        test = subprocess.run(command, cwd=checkout, capture_output=True, text=True)
        assert test.returncode == 0, test.stdout + test.stderr
        tool = "Codex" if payload["repair_agent"] == "codex" else "OpenCode"
        assert f"AI assistance: {tool}" in config["body"]
        assert "Floor division truncates" in config["body"]
        assert "test_regression.py" in config["body"] and "Tests:" in config["body"]
        assert config["verified_patch_sha256"] == hashlib.sha256(patch_file.read_bytes()).hexdigest()
        return {"ok": True, "pr": "https://github.com/pypa/pipx/pull/123"}

    monkeypatch.setattr(publish_remote, "publish", publish)
    assert publish_remote.publish_from_env(payload, repo, out)["ok"]


def test_real_publication_stages_only_the_verified_files(delivered, monkeypatch):
    repo, out, payload, _ = delivered
    (repo / "accidental-output.txt").write_text("must stay local", encoding="utf-8")
    payload["publish"].update(push_repo="fixture/fork", branch="fix/regression", base_branch="main")
    real_run = publish_remote._run

    def run(args, cwd=None):
        if args[:2] == ["gh", "pr"]:
            value = "[]" if "list" in args else "https://github.com/pypa/pipx/pull/123"
            return subprocess.CompletedProcess(args, 0, value, "")
        if args[:2] == ["git", "push"]:
            files = real_run(["git", "show", "--pretty=", "--name-only", "HEAD"], cwd).stdout
            assert "test_regression.py" in files and "calc.py" in files
            assert "accidental-output.txt" not in files
            return subprocess.CompletedProcess(args, 0, "", "")
        return real_run(args, cwd)

    monkeypatch.setattr(publish_remote, "_ensure_fork", lambda *args: "main")
    monkeypatch.setattr(publish_remote, "_run", run)
    assert publish_remote.publish_from_env(payload, repo, out)["ok"]


@pytest.mark.parametrize("corruption", ["patch", "lineage", "missing_test", "failed"])
def test_publisher_rejects_corrupted_or_unverified_delivery(delivered, monkeypatch, corruption):
    repo, out, payload, _ = delivered
    report_path = out / "test-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if corruption == "patch":
        with (out / "change.patch").open("ab") as handle:
            handle.write(b"\n# unexpected change\n")
    elif corruption == "lineage":
        report["base_sha"] = "wrong"
    elif corruption == "missing_test":
        report["regression_test_files"] = ["missing_test.py"]
    else:
        report["failure_kind"] = "additional_check_failed"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setattr(publish_remote, "publish", lambda *args: pytest.fail("must not publish"))
    assert publish_remote.publish_from_env(payload, repo, out)["ok"] is False


@pytest.mark.parametrize("path", ["yarn.lock", "sub/Cargo.lock", ".github/workflows/ci.yml"])
def test_incidental_dependency_and_ci_changes_block_before_verification(tmp_path, monkeypatch, path):
    repo, sha = make_repo(tmp_path)
    patch = f"diff --git a/{path} b/{path}\n--- /dev/null\n+++ b/{path}\n@@ -0,0 +1 @@\n+changed\n"
    assert check_delivery_scope(patch)
    monkeypatch.setattr(repair, "_run_cmd", lambda *args: pytest.fail("must stop before building"))
    result = repair.run_repair(
        Job(mode="repair", repo="pypa/pipx", base_sha=sha, idempotency_key=sha), repo, tmp_path / "out",
        test_cmd=[sys.executable, "-m", "pytest"], patch_producer=lambda *args: (patch, []))
    assert result.outcome == "blocked"


def test_setup_cannot_change_the_patch_after_verification_started(tmp_path, monkeypatch):
    from test_p5_repair import GOOD_PATCH

    repo, sha = make_repo(tmp_path)

    def verify(command, work, env):
        source = work / "calc.py"
        if "//" in source.read_text(encoding="utf-8"):
            return 1, "test_div assertion failed", ""
        source.write_text("def div(a, b):\n    return 0\n", encoding="utf-8")
        return 0, "passed", ""

    monkeypatch.setattr(repair, "_run_cmd", verify)
    result = repair.run_repair(
        Job(mode="repair", repo="pypa/pipx", base_sha=sha, idempotency_key=sha), repo, tmp_path / "out",
        test_cmd=[sys.executable, "-m", "pytest"], patch_producer=lambda *args: (GOOD_PATCH, []))
    assert result.outcome == "failed"
    assert "delivery_changed_during_verification" in result.unverified_items[0]
