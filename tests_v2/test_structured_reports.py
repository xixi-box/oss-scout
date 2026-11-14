import json
import sys

import pytest

from scout_v2.workers import repair, test_reports
from test_p5_repair import GOOD_PATCH, job, make_repo


PLAN = {"setup_steps": [], "base_failure": ["test_div", "assert 3 == 3.5"],
        "patched_success": ["structured report provides the success evidence"],
        "test_report": {"format": "junit", "path": "regression.xml", "case": "test_div"}}


def xml(status="passed", name="test_div", message="assert 3 == 3.5"):
    element = {"failed": "failure", "error": "error", "skipped": "skipped"}.get(status)
    detail = f'<{element} message="{message}"/>' if element else ""
    return f'<testsuites><testsuite><testcase classname="test_calc" name="{name}">{detail}</testcase></testsuite></testsuites>'


def test_real_pytest_report_replays_without_console_markers(tmp_path):
    repo, sha = make_repo(tmp_path)
    result = repair.run_repair(job(sha), repo, tmp_path / "out", patch_producer=lambda *_: {
        "patch": GOOD_PATCH, "test_cmd": [sys.executable, "-m", "pytest", "test_div.py", "--junitxml=regression.xml", "-q"],
        "test_files": {"test_div.py": (repo / "test_div.py").read_text()}, "verification_plan": PLAN,
    })
    assert result.outcome == "succeeded" and result.reproduced_on_base and result.fixed_on_patch
    report = json.loads((tmp_path / "out/test-report.json").read_text())
    assert report["base_evidence"]["case"]["status"] == "failed"
    assert report["patched_evidence"]["case"]["status"] == "passed"
    assert report["base_evidence"]["case"]["id"] == report["patched_evidence"]["case"]["id"]
    assert (tmp_path / "out/base-regression.junit.xml").exists()


@pytest.mark.parametrize("status", ["passed", "skipped", "error"])
def test_base_requires_actual_assertion_failure(tmp_path, status):
    (tmp_path / "regression.xml").write_text(xml(status))
    evidence = test_reports.collect_report(tmp_path, tmp_path, PLAN, "base")
    with pytest.raises(ValueError):
        test_reports.check_report(evidence, PLAN, "base")


@pytest.mark.parametrize("status", ["failed", "skipped", "error"])
def test_patch_cannot_skip_or_fail_target(tmp_path, status):
    (tmp_path / "regression.xml").write_text(xml("failed"))
    base = test_reports.collect_report(tmp_path, tmp_path, PLAN, "base")
    (tmp_path / "regression.xml").write_text(xml(status))
    patched = test_reports.collect_report(tmp_path, tmp_path, PLAN, "patched")
    with pytest.raises(ValueError):
        test_reports.check_report(patched, PLAN, "patched", base)


def test_target_identity_and_bug_symptom_must_match(tmp_path):
    (tmp_path / "regression.xml").write_text(xml("failed", message="unrelated assertion"))
    base = test_reports.collect_report(tmp_path, tmp_path, PLAN, "base")
    with pytest.raises(ValueError, match="target bug"):
        test_reports.check_report(base, PLAN, "base")
    (tmp_path / "regression.xml").write_text(xml(name="another_case"))
    with pytest.raises(ValueError, match="missing"):
        test_reports.collect_report(tmp_path, tmp_path, PLAN, "patched")


@pytest.mark.parametrize("content", ["bad xml", "<!DOCTYPE x><testsuites/>",
    '<testsuite><testcase name="test_div"/><testcase name="test_div"/></testsuite>',
    '<testsuite><testcase name="test_div"><failure/><skipped/></testcase></testsuite>'])
def test_invalid_reports_do_not_pass(tmp_path, content):
    (tmp_path / "regression.xml").write_text(content)
    with pytest.raises(ValueError):
        test_reports.collect_report(tmp_path, tmp_path, PLAN, "patched")


def test_stale_report_is_deleted_before_test(tmp_path):
    (tmp_path / "regression.xml").write_text(xml())
    test_reports.prepare_report(tmp_path, PLAN)
    with pytest.raises(ValueError, match="missing"):
        test_reports.collect_report(tmp_path, tmp_path, PLAN, "patched")


@pytest.mark.parametrize("path", ["../escape.xml", "/tmp/out.xml", "C:/out.xml", "x\\report.xml"])
def test_report_paths_cannot_escape(path):
    plan = {**PLAN, "test_report": {**PLAN["test_report"], "path": path}}
    assert not repair._verification_plan_valid(plan)


@pytest.mark.parametrize("message", ["Cannot find module './new-helper.js'", "error[E0425]: unresolved name",
                                    "pytest: command not found"])
def test_hidden_environment_error_is_not_a_bug_assertion(tmp_path, message):
    (tmp_path / "regression.xml").write_text(xml("failed", message=message))
    plan = {**PLAN, "base_failure": ["test_div", message]}
    evidence = test_reports.collect_report(tmp_path, tmp_path, plan, "base")
    with pytest.raises(ValueError, match="environment/build"):
        test_reports.check_report(evidence, plan, "base")


@pytest.mark.parametrize("content", [
    '<testsuite tests="1" failures="1"><testcase name="test_div"/></testsuite>',
    '<testsuite tests="2"><testcase name="test_div"/></testsuite>',
    '<testsuite errors="1"><testcase name="test_div"/></testsuite>',
])
def test_incomplete_suite_totals_are_rejected(tmp_path, content):
    (tmp_path / "regression.xml").write_text(content)
    with pytest.raises(ValueError, match="counts"):
        test_reports.collect_report(tmp_path, tmp_path, PLAN, "patched")


def test_suppressed_case_is_never_a_pass(tmp_path):
    (tmp_path / "regression.xml").write_text('<testsuite><testcase name="test_div" status="notrun" result="suppressed"/></testsuite>')
    evidence = test_reports.collect_report(tmp_path, tmp_path, PLAN, "patched")
    assert evidence["case"]["status"] == "skipped"


def test_namespaced_junit_keeps_case_identity(tmp_path):
    (tmp_path / "regression.xml").write_text(xml("failed").replace("<testsuites>", '<testsuites xmlns="urn:junit">'))
    evidence = test_reports.collect_report(tmp_path, tmp_path, PLAN, "base")
    test_reports.check_report(evidence, PLAN, "base")
    assert evidence["case"]["id"] == "test_calc::test_div"


def test_agent_generated_xml_is_not_delivered_as_source(tmp_path):
    repo, sha = make_repo(tmp_path)
    spec = {"format": "junit", "path": "./regression.xml", "case": "test_div"}

    class Agent:
        def run(self, *_):
            (self.workdir / "calc.py").write_text("def div(a, b):\n    return a / b\n")
            (self.workdir / "test_new_regression.py").write_text("from calc import div\ndef test_div():\n    assert div(7, 2) == 3.5\n")
            (self.workdir / "regression.xml").write_text("<testsuite/>")
            (self.workdir / repair.RESULT_FILE).write_text(json.dumps({
                "test_file": "test_new_regression.py", "test_cmd": [sys.executable, "-m", "pytest", "test_new_regression.py", "--junitxml=regression.xml", "-q"],
                "setup_steps": [], "base_failure": ["test_div", "assert 3 == 3.5"],
                "patched_success": [], "test_report": spec,
            }))
            return True, "done"

    def producer(current_job, work, context):
        agent = Agent()
        agent.workdir = work
        return repair.opencode_patch_producer(current_job, work, context, client=agent, model="test")

    result = repair.run_repair(job(sha), repo, tmp_path / "out", patch_producer=producer)
    assert result.outcome == "succeeded"
    patch = (tmp_path / "out/change.patch").read_text()
    assert "regression.xml" not in patch
    assert (tmp_path / "out/base-regression.junit.xml").exists()
    assert (tmp_path / "out/patched-regression.junit.xml").exists()
