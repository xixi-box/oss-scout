"""Replay real Actions output; never infer success from a zero exit code alone."""
import copy
import json
from pathlib import Path

import pytest

from scout_v2.workers import repair
from tests_v2.test_p5_repair import GOOD_PATCH, job, make_repo


def recorded(name):
    return json.loads((Path(__file__).parent / "fixtures/repair_logs" / f"{name}.json").read_text(encoding="utf-8"))


def replay(tmp_path, monkeypatch, evidence):
    repo, sha = make_repo(tmp_path)
    plan = copy.deepcopy(evidence["verification_plan"])
    plan["setup_steps"] = []  # Test evidence parsing, not third-party dependency installation.
    calls = []

    def run(cmd, work, env):
        stage = "base" if "//" in (work / "calc.py").read_text() else "patched"
        calls.append(stage)
        return (evidence[f"{stage}_returncode"], evidence[f"{stage}_stdout_tail"],
                evidence.get(f"{stage}_stderr_tail", ""))

    monkeypatch.setattr(repair, "_run_cmd", run)
    path = "tests/cron/test_receipt_recovery_profile_scope.py"
    result = repair.run_repair(job(sha), repo, tmp_path / "out", patch_producer=lambda *_: {
        "patch": GOOD_PATCH, "test_cmd": evidence["test_cmd"],
        "test_files": {path: "# regression fixture\n"}, "verification_plan": plan,
    })
    return result, calls


def test_real_hermes_file_summary_confirms_same_regression(tmp_path, monkeypatch):
    data = recorded("hermes_receipt_recovery")
    result, calls = replay(tmp_path, monkeypatch, data)
    assert calls == ["base", "patched"]
    assert result.outcome == "succeeded"
    assert result.reproduced_on_base and result.fixed_on_patch
    assert result.regression_tests_passed
    assert data["patched_stdout_tail"] in (tmp_path / "out/verification.log").read_text(encoding="utf-8")


@pytest.mark.parametrize("change,expected", [
    ("base_passes", "base_test_passed"),
    ("unrelated_bug", "base_failure_unconfirmed"),
    ("patched_fails", "patched_test_failed"),
    ("other_file", "patched_test_unconfirmed"),
    ("filtered_command", "patched_test_unconfirmed"),
    ("skipped_case", "patched_test_unconfirmed"),
    ("different_count", "patched_test_unconfirmed"),
    ("no_file_result", "patched_test_unconfirmed"),
])
def test_wrapper_summary_does_not_weaken_release_gate(tmp_path, monkeypatch, change, expected):
    data = recorded("hermes_receipt_recovery")
    if change == "base_passes":
        data["base_returncode"] = 0
    elif change == "unrelated_bug":
        data["base_stdout_tail"] = "unrelated assertion failure"
    elif change == "patched_fails":
        data["patched_returncode"] = 1
    elif change == "other_file":
        data["patched_stdout_tail"] = data["patched_stdout_tail"].replace("test_receipt_recovery_profile_scope.py", "test_other.py")
    elif change == "filtered_command":
        data["test_cmd"][-1] += " -k unrelated"
    elif change == "skipped_case":
        data["patched_stdout_tail"] += "1 skipped\n"
    elif change == "different_count":
        data["patched_stdout_tail"] = data["patched_stdout_tail"].replace("(2✓,", "(1✓,")
    elif change == "no_file_result":
        data["patched_stdout_tail"] = "2 tests passed, 0 failed\n"
    result, _ = replay(tmp_path, monkeypatch, data)
    assert result.outcome in ("failed", "blocked") and not result.fixed_on_patch
    assert result.unverified_items[0].startswith(expected)


def test_real_pydantic_parallel_plugin_does_not_hide_executed_tests():
    data = recorded("pydantic_parallel_collection")
    stdout = data["base_stdout_tail"]
    assert "Collected 0 items to run in parallel" in stdout
    assert "60 failed, 31 passed, 94 deselected" in stdout
    assert repair._verification_error(1, stdout, data["base_stderr_tail"]) is None
    assert repair._evidence_matches(data["verification_plan"]["base_failure"], stdout, "")


@pytest.mark.parametrize("patched_passes", [True, False])
def test_pydantic_real_base_still_requires_patched_success(tmp_path, monkeypatch, patched_passes):
    data = recorded("pydantic_parallel_collection")
    # The historical run stopped at base. Simulate each possible patched outcome;
    # this verifies the gate and does not claim the historical patch was tested.
    data["patched_returncode"] = 0 if patched_passes else 1
    data["patched_stdout_tail"] = (
        "\n".join(data["verification_plan"]["patched_success"]) if patched_passes
        else data["base_stdout_tail"])
    result, calls = replay(tmp_path, monkeypatch, data)
    assert calls == ["base", "patched"]
    assert result.reproduced_on_base
    assert result.fixed_on_patch is patched_passes
    assert result.outcome == ("succeeded" if patched_passes else "failed")


@pytest.mark.parametrize("output", [
    "collected 0 items", "Collected 0 items to run in parallel",
    "collected 0 items\nno tests ran in 0.01s",
    "Collected 0 items to run in parallel\n=== 2 skipped in 0.1s ===",
    "collected 0 items\n=== 1 passed in 0.1s ===",
])
def test_actual_empty_collection_still_blocks(output):
    assert repair._verification_error(1, output, "")[0] == "command_error"


def test_parallel_diagnostic_does_not_hide_other_environment_errors():
    data = recorded("pydantic_parallel_collection")
    assert repair._verification_error(1, data["base_stdout_tail"], "No module named pytest")[0] == "missing_dependency"
