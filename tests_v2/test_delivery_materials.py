"""Managed OpenCode/Codex handoffs recover observations, never fabricate proof."""
import json
import subprocess
import sys

import pytest

from scout_v2.workers import repair
from scout_v2.workers.delivery_materials import coverage_issues, material_issues, reported_blocker
from test_p5_repair import make_repo, job


def complete_report():
    return {"test_file": "test_regression.py", "test_cmd": [sys.executable.replace(chr(92), "/"),
        "-m", "pytest", "test_regression.py", "-v", "--junitxml=regression.xml"],
        "setup_steps": [], "base_failure": ["test_fractional", "assert 3 == 3.5"],
        "patched_success": [], "test_report": {"format": "junit", "path": "regression.xml", "case": "test_fractional"},
        "root_cause": "Floor division truncates fractional results.",
        "pr_title": "fix: preserve fractional division results",
        "pr_body": "Fix floor division.\n\n{VERIFIED_TESTS}",
        "coverage": {"entrypoint": {"path": "calc.py", "symbol": "div"},
                     "call_chain": [{"path": "calc.py", "symbol": "div"}],
                     "regression_case": "test_fractional",
                     "scenarios": ["fractional operands through the real div entrypoint"],
                     "limitations": ["unrelated package tests not exercised"]}}


def install_client(monkeypatch, *, mode="recover", clock=None):
    instances = []

    class Client:
        def __init__(self, **kwargs):
            self.workdir = kwargs["workdir"]
            self.timeout = kwargs["timeout_seconds"]
            self.calls = []
            instances.append(self)

        def run(self, model, prompt):
            self.calls.append((prompt, self.timeout))
            report = complete_report()
            if len(self.calls) == 1:
                self.original = (self.workdir / "calc.py").read_text()
                (self.workdir / "calc.py").write_text("def div(a, b):\n    return a / b\n")
                (self.workdir / "test_regression.py").write_text(
                    "from calc import div\ndef test_fractional():\n    assert div(7, 2) == 3.5\n")
                if mode != "complete":
                    report["base_failure"] = []  # Wasmtime #15297's real missing assertion shape.
                    report.pop("coverage")
                if clock is not None:
                    clock[0] += 2401
            elif mode == "recover":
                check = json.loads((self.workdir / "OSSCOUT_DELIVERY_CHECK.json").read_text())
                assert any("verification_plan" in reason for reason in check["missing_or_invalid"])
                assert "Never label dependency/import/build/collection" in prompt
                # Material was absent, so actually rerun the original and patched
                # versions rather than fill assertion strings from a guess.
                patched = (self.workdir / "calc.py").read_text()
                try:
                    (self.workdir / "calc.py").write_text(self.original)
                    base = subprocess.run(report["test_cmd"], cwd=self.workdir, capture_output=True, text=True)
                finally:
                    (self.workdir / "calc.py").write_text(patched)
                fixed = subprocess.run(report["test_cmd"], cwd=self.workdir, capture_output=True, text=True)
                assert base.returncode == 1 and fixed.returncode == 0
                assert report["base_failure"][1] in base.stdout
                (self.workdir / "OSSCOUT_BASE.log").write_text(base.stdout)
                (self.workdir / "OSSCOUT_PATCHED.log").write_text(fixed.stdout)
            elif mode != "completion_failed":
                report["base_failure"] = []
                report.pop("coverage")
            (self.workdir / repair.RESULT_FILE).write_text(json.dumps(report))
            if mode == "completion_failed" and len(self.calls) > 1:
                return False, ""
            if mode == "communication_failure":
                from scout_v2.workers.codex_client import CODEX_COMMUNICATION_RETRY_PREFIX
                return False, CODEX_COMMUNICATION_RETRY_PREFIX + " test heartbeat error"
            return True, "Agent's actual final response"

    monkeypatch.setattr(repair, "OpenCodeClient", Client)
    monkeypatch.setattr("scout_v2.workers.codex_client.CodexClient", Client)
    return instances


@pytest.mark.parametrize("agent", ["opencode", "codex"])
def test_managed_completion_reruns_real_tests_and_still_independently_replays(tmp_path, monkeypatch, agent):
    repo, sha = make_repo(tmp_path)
    task = job(sha)
    task.repair_agent, task.model = agent, "fixture"
    clients = install_client(monkeypatch)
    out = tmp_path / "out"
    result = repair.run_repair(task, repo, out)
    assert result.outcome == "succeeded", result.unverified_items
    assert len(clients) == 1 and len(clients[0].calls) == 2
    assert clients[0].calls[0][1] == 2100
    assert 0 < clients[0].calls[1][1] <= 300
    report = json.loads((out / "test-report.json").read_text())
    assert report["base_returncode"] == 1 and report["patched_returncode"] == 0
    assert report["coverage"]["entrypoint"]["path"] == "calc.py"
    assert report["delivery_preflight"]["attempts"] == 1
    assert not report["delivery_preflight"]["remaining_issues"]
    assert "assert 3 == 3.5" in (out / "OSSCOUT_BASE.log").read_text()
    assert "passed" in (out / "OSSCOUT_PATCHED.log").read_text()
    patch = (out / "change.patch").read_text()
    assert "OSSCOUT_" not in patch and "regression.xml" not in patch
    assert "calc.py" in patch and "test_regression.py" in patch


@pytest.mark.parametrize("agent", ["opencode", "codex"])
def test_incomplete_materials_get_only_one_completion_pass(tmp_path, monkeypatch, agent):
    repo, sha = make_repo(tmp_path)
    task = job(sha)
    task.repair_agent, task.model = agent, "fixture"
    clients = install_client(monkeypatch, mode="still_missing")
    out = tmp_path / "out"
    result = repair.run_repair(task, repo, out)
    assert result.outcome == "failed" and "delivery_materials_incomplete" in result.unverified_items[0]
    assert len(clients[0].calls) == 2
    assert not (out / "change.patch").exists()
    diagnostic = json.loads((out / "delivery-preflight.json").read_text())
    assert diagnostic["attempts"] == 1 and diagnostic["remaining_issues"]


def test_complete_handoff_does_not_trigger_another_agent_run(tmp_path, monkeypatch):
    repo, sha = make_repo(tmp_path)
    clients = install_client(monkeypatch, mode="complete")
    result = repair.opencode_patch_producer(job(sha), repo, "fixture", model="fixture")
    assert len(clients[0].calls) == 1
    assert result["delivery_preflight"] == {"initial_issues": [], "attempts": 0, "remaining_issues": []}


def test_expired_total_budget_does_not_start_completion(tmp_path, monkeypatch):
    repo, sha = make_repo(tmp_path)
    clock = [100.0]
    monkeypatch.setattr(repair.time, "monotonic", lambda: clock[0])
    clients = install_client(monkeypatch, clock=clock)
    with pytest.raises(RuntimeError, match="delivery_materials_incomplete"):
        repair.opencode_patch_producer(job(sha), repo, "fixture", model="fixture")
    assert len(clients[0].calls) == 1


def test_communication_failure_keeps_retry_classification_without_completion(tmp_path, monkeypatch):
    from scout_v2.workers.codex_client import CODEX_COMMUNICATION_RETRY_PREFIX
    repo, sha = make_repo(tmp_path)
    clients = install_client(monkeypatch, mode="communication_failure")
    with pytest.raises(RuntimeError, match=CODEX_COMMUNICATION_RETRY_PREFIX):
        repair.opencode_patch_producer(job(sha), repo, "fixture", model="fixture")
    assert len(clients[0].calls) == 1


def test_failed_completion_cannot_be_promoted_by_a_complete_result_file(tmp_path, monkeypatch):
    repo, sha = make_repo(tmp_path)
    clients = install_client(monkeypatch, mode="completion_failed")
    result = repair.run_repair(job(sha), repo, tmp_path / "out")
    assert result.outcome == "failed" and not result.reproduced_on_base
    assert "did not complete successfully" in result.unverified_items[0]
    assert len(clients[0].calls) == 2


@pytest.mark.parametrize("field,value", [("entrypoint", {"path": "../secret", "symbol": "div"}),
    ("call_chain", []), ("regression_case", "different_case"), ("scenarios", []), ("limitations", None)])
def test_coverage_requires_safe_existing_references_and_declared_case(tmp_path, field, value):
    repo, _ = make_repo(tmp_path)
    report = complete_report()
    report["coverage"][field] = value
    assert coverage_issues(report["coverage"], repo, report)


def test_malformed_agent_material_is_reported_without_crashing(tmp_path):
    repo, _ = make_repo(tmp_path)
    report = complete_report()
    report.update(base_failure=3, test_report=None)
    assert material_issues(report, repo)


def test_new_diagnostic_logs_remove_known_credentials_and_bearer_tokens(monkeypatch):
    from scout_v2.workers.delivery_materials import redact_log
    monkeypatch.setenv("GH_TOKEN", "github-secret-value")
    monkeypatch.setenv("CF_INGEST_TOKEN", "broker-secret-value")
    monkeypatch.setenv("OPENCODE_API_KEY", "model-secret-value")
    text = 'github-secret-value broker-secret-value model-secret-value Authorization: Bearer token-ABC.123 ' \
           '{"refresh_token":"rotating-secret","access_token":"access-secret"} assertion failed'
    result = redact_log(text)
    assert all(secret not in result for secret in ("github-secret-value", "broker-secret-value", "model-secret-value",
                                                  "token-ABC.123", "rotating-secret", "access-secret"))
    assert "assertion failed" in result


def test_opencode_transient_retries_share_one_timeout_budget(tmp_path, monkeypatch):
    from scout_v2.workers import opencode_client
    clock = [0.0]
    timeouts = []
    monkeypatch.setattr(opencode_client.time, "monotonic", lambda: clock[0])

    def run(*args, **kwargs):
        timeouts.append(kwargs["timeout"])
        clock[0] += 8 if len(timeouts) == 1 else 2
        return subprocess.CompletedProcess(args, 1, "", "TLS handshake failed")

    monkeypatch.setattr(opencode_client.subprocess, "run", run)
    ok, detail = opencode_client.OpenCodeClient(timeout_seconds=10, workdir=tmp_path).run("fixture", "task")
    assert not ok and "retry budget exhausted" in detail
    assert timeouts == [10, 2]


@pytest.mark.parametrize("agent", ["opencode", "codex"])
def test_explicit_inaccessible_reproducer_blocker_is_not_rewritten_as_missing_material(tmp_path, monkeypatch, agent):
    # Docling #1473 reported a Box sign-in wall, not a forgotten test command.
    repo, sha = make_repo(tmp_path)
    task = job(sha)
    task.repair_agent = agent
    calls = []

    class Client:
        def __init__(self, **kwargs):
            self.workdir = kwargs["workdir"]
        def run(self, *_):
            calls.append(1)
            (self.workdir / repair.RESULT_FILE).write_text(json.dumps({
                "status": "blocked", "publication_ready": False,
                "blocker": "Named PDF input redirects to Box | Login; expected table grid unavailable.",
                "base_failure": [], "patched_success": []}))
            return True, "Blocked: inaccessible reproducer"

    monkeypatch.setattr(repair, "OpenCodeClient", Client)
    monkeypatch.setattr("scout_v2.workers.codex_client.CodexClient", Client)
    out = tmp_path / "out"
    result = repair.run_repair(task, repo, out)
    assert len(calls) == 1
    assert "agent_reported_blocker:" in result.unverified_items[0]
    assert "Box | Login" in result.unverified_items[0]
    assert "delivery_materials_incomplete" not in result.unverified_items[0]
    assert json.loads((out / "agent-handoff.json").read_text())["publication_ready"] is False
    assert not (out / "change.patch").exists()


@pytest.mark.parametrize("handoff", [
    {"status": "blocked", "publication_ready": False,
     "blocker": "Named PDF input redirects to Box | Login; expected grid unavailable."},
    {"status": "blocked_no_target_reproduction", "publication_allowed": False,
     "blockers": ["Both original and final CSV characterization runs pass; no target assertion observed."]},
    {"status": "blocked", "publication_ready": False,
     "blockers": ["Unchanged HTTP 404 regression already passes; issue discussion reports the fix."]},
    {"status": "blocked_no_reproduced_target_failure", "publication_ready": False,
     "blocker": "Unchanged attachment retains images and chart data; no target failure reproduced."},
])
def test_nov17_actual_blocker_report_shapes_survive_the_reader(tmp_path, handoff):
    (tmp_path / repair.RESULT_FILE).write_text(json.dumps(handoff))
    assert reported_blocker(repair._read_result_file(tmp_path))


def test_missing_material_without_an_explicit_blocker_still_gets_completion():
    assert not reported_blocker({"base_failure": [], "test_cmd": ""})
    assert not reported_blocker({"status": "blocked", "publication_ready": False})
