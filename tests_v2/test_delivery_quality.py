"""Exercise mechanical delivery for agent reports and legacy in-flight artifacts."""
import hashlib
import json
import subprocess
import sys

import pytest

from scout_v2.schemas import Job, Result
from scout_v2.workers import publish_remote, repair
from scout_v2.workers.quality_gate import check_delivery_scope
from test_p5_repair import make_repo


@pytest.fixture(autouse=True)
def isolate_publication_policy(monkeypatch):
    # This file tests patch delivery; live policy and review binding have their
    # own integration/negative tests rather than contacting external repositories.
    monkeypatch.setattr(publish_remote, "refresh_publication_evidence", lambda *_: {"ok": True, "snapshot": {}})


@pytest.fixture(params=["opencode", "codex"])
def delivered(request, tmp_path):
    repo, sha = make_repo(tmp_path)
    job = Job(mode="repair", repo="pypa/pipx", base_sha=sha,
              idempotency_key=f"repair:{sha}", repair_agent=request.param)
    command = [sys.executable.replace(chr(92), "/"), "-m", "pytest", "test_regression.py", "-v"]

    (repo / "calc.py").write_text("def div(a, b):\n    return a / b\n", encoding="utf-8")
    (repo / "test_regression.py").write_text(
        "from calc import div\ndef test_fractional_division():\n    assert div(7, 2) == 3.5\n", encoding="utf-8")
    subprocess.run(["git", "add", "-N", "test_regression.py"], cwd=repo, check=True)
    patch = subprocess.check_output(["git", "diff", "--binary"], cwd=repo)
    subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=repo, check=True, capture_output=True)
    out = tmp_path / "out"
    out.mkdir()
    (out / "change.patch").write_bytes(patch)
    result = Result(job_id=job.job_id, backend="github_actions", outcome="succeeded", base_sha=sha,
                    reproduced_on_base=True, fixed_on_patch=True, regression_tests_passed=True)
    (out / "result.json").write_text(json.dumps(result.to_dict()), encoding="utf-8")
    report = {"job_id": job.job_id, "base_sha": sha, "base_returncode": 1, "patched_returncode": 0,
              "delivery_patch_sha256": hashlib.sha256(patch).hexdigest(),
              "delivery_files": ["calc.py", "test_regression.py"],
              "regression_test_files": ["test_regression.py"], "test_cmd": command,
              "root_cause": "Floor division truncates fractional results.",
              "pr_title": "fix: preserve fractional division results",
              "pr_body": "## Problem\nFloor division truncates fractional results.\n\n## Changes\nUse true division for fractional results.\n\n## Tests\n{VERIFIED_TESTS}"}
    (out / "test-report.json").write_text(json.dumps(report), encoding="utf-8")
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
        assert "AI assistance:" not in config["body"]
        assert "oss-scout independently replayed" not in config["body"]
        assert "Floor division truncates" in config["body"]
        assert config["title"] == "fix: preserve fractional division results"
        assert "## Changes" in config["body"] and "Use true division" in config["body"]
        assert "{VERIFIED_TESTS}" not in config["body"]
        assert "test_regression.py" in config["body"] and "Tests:" in config["body"]
        assert config["verified_patch_sha256"] == hashlib.sha256(patch_file.read_bytes()).hexdigest()
        return {"ok": True, "pr": "https://github.com/pypa/pipx/pull/123"}

    monkeypatch.setattr(publish_remote, "publish", publish)
    assert publish_remote.publish_from_env(payload, repo, out)["ok"]


def test_real_publication_stages_only_the_verified_files(delivered, monkeypatch):
    repo, out, payload, _ = delivered
    for kind in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{kind}_NAME", payload["repair_agent"])
        monkeypatch.setenv(f"GIT_{kind}_EMAIL", "tool@example.invalid")
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
            identity = real_run(["git", "show", "-s", "--format=%an|%ae|%cn|%ce"], cwd).stdout.strip()
            expected = "wangshun|96341168+xixi-box@users.noreply.github.com"
            assert identity == expected + "|" + expected
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
    result = repair.run_repair(
        Job(mode="repair", repo="pypa/pipx", base_sha=sha, idempotency_key=sha), repo, tmp_path / "out",
        test_cmd=[sys.executable, "-m", "pytest"], patch_producer=lambda *args: (patch, []))
    assert result.outcome == "blocked"



@pytest.fixture
def agent_delivery(delivered):
    repo, out, payload, command = delivered
    path = out / "test-report.json"
    report = json.loads(path.read_text())
    for key in ("base_returncode", "patched_returncode", "regression_test_files"):
        report.pop(key, None)
    report.update(verification_mode="agent", schema_version=2, agent_report={"status": "success"})
    path.write_text(json.dumps(report))
    path = out / "result.json"
    result = json.loads(path.read_text())
    for key in ("reproduced_on_base", "fixed_on_patch", "regression_tests_passed"):
        result.pop(key, None)
    result["verification_mode"] = "agent"
    path.write_text(json.dumps(result))
    return repo, out, payload, command


def test_agent_delivery_publishes_without_independent_evidence_or_review(agent_delivery, monkeypatch):
    repo, out, payload, _ = agent_delivery
    monkeypatch.setattr(publish_remote, "publish", lambda *_: {"ok": True, "pr": "https://github.com/pypa/pipx/pull/123"})
    assert publish_remote.publish_from_env(payload, repo, out)["ok"]
    assert not (out / "code-review.json").exists()
    assert (out / "verified-delivery/manifest.json").exists()


def test_publisher_does_not_replace_agent_self_check_with_report_semantics(agent_delivery, monkeypatch):
    repo, out, payload, _ = agent_delivery
    path = out / "test-report.json"
    report = json.loads(path.read_text())
    for field in ("root_cause", "test_cmd", "coverage", "agent_report"):
        report.pop(field, None)
    path.write_text(json.dumps(report))
    monkeypatch.setattr(publish_remote, "publish", lambda *_: {"ok": True})
    assert publish_remote.publish_from_env(payload, repo, out)["ok"]


def test_rclone_agent_delivery_uses_the_same_mechanical_publication_gate(agent_delivery, monkeypatch):
    repo, out, payload, _ = agent_delivery
    payload["repo"] = payload["publish"]["upstream"] = "rclone/rclone"
    monkeypatch.setattr("scout_v2.roster.monitored_repo", lambda value: value == "rclone/rclone")
    def publish(*args):
        assert "Additional checks passed" not in args[2]["body"]
        return {"ok": True}
    monkeypatch.setattr(publish_remote, "publish", publish)
    assert publish_remote.publish_from_env(payload, repo, out)["ok"]


def test_agent_quality_gate_keeps_lineage_without_old_result_flags():
    from scout_v2.workers.quality_gate import verify_result
    job = Job(job_id="job-agent", base_sha="a" * 40)
    result = Result(job_id=job.job_id, outcome="succeeded", base_sha=job.base_sha,
                    verification_mode="agent", fixed_on_patch=True)
    assert verify_result(result, job).ok
    assert not verify_result(result, job, expected_base_sha="b" * 40).ok
    assert Result.from_dict(result.to_dict()).verification_mode == "agent"


def test_agent_archive_and_recovery_need_no_independent_junit(agent_delivery, tmp_path, monkeypatch):
    from scout_v2.workers import publication_guard, delivery_recovery
    repo, out, payload, _ = agent_delivery
    payload.update(source_run="12345", runner_repo="xixi-box/oss-scout")
    saved = publication_guard.preserve_verified_bundle(payload, out, {"ok": False})
    assert saved["ok"], saved
    source = {"job_id": payload["job_id"], "run_id": "12345", "repository": "xixi-box/oss-scout",
              "patch_sha256": saved["patch_sha256"]}
    from pathlib import Path
    bundle = Path(saved["path"])
    files, _ = delivery_recovery.load_bundle(bundle, source, repo=payload["repo"],
                                             base_sha=payload["base_sha"], issue_number=123)
    monkeypatch.setenv("OSSCOUT_VERIFIED_DELIVERY_DIR", str(bundle))
    job = Job.from_dict({**payload, "job_id": "job-retry", "verified_delivery_source": source})
    produced = delivery_recovery.recovery_producer(job, repo, "")
    assert produced["patch"].encode("utf-8") == files["change.patch"]
    assert produced["agent_report"] == {"status": "success"}
    collected = {"result": json.loads(files["result.json"]), "test_report": json.loads(files["test-report.json"]),
                 "patch": files["change.patch"]}
    controller = tmp_path / "controller"
    controller.mkdir()
    retained = publication_guard.retain_collected_delivery(controller, payload, collected)
    assert retained["ok"] and not retained["raw_reports_missing"]
    assert not retained["manifest"]["requires_reverification"]
