"""PR 监控分类的单元测试。

PR 开出去之后没人管是最容易烂尾的地方。分类逻辑决定"要不要动手"，判错的
代价是双向的：漏判＝PR 烂掉，误判＝对着机器人的客套话瞎忙一轮。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import importlib.util  # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    "monitor_prs", Path(__file__).resolve().parents[1] / "scripts" / "monitor_prs.py")
mp = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mp)


def _pr(comments=(), rollup=(), **kw):
    base = {"comments": list(comments), "statusCheckRollup": list(rollup),
            "reviewDecision": "", "isDraft": False}
    base.update(kw)
    return base


def _check(name, conclusion, status=""):
    return {"name": name, "conclusion": conclusion, "status": status}


def _comment(login, body):
    return {"author": {"login": login}, "body": body}


class TestCiFailure:
    def test_failing_check_triggers_fix(self):
        got = mp.classify(_pr(rollup=[_check("tests", "FAILURE")]))
        assert got["action"] == "fix_ci"
        assert "tests" in got["failing_checks"]

    def test_cancelled_counts_as_failure(self):
        got = mp.classify(_pr(rollup=[_check("build", "CANCELLED")]))
        assert got["action"] == "fix_ci"

    def test_pending_is_wait(self):
        got = mp.classify(_pr(rollup=[_check("tests", "", "IN_PROGRESS")]))
        assert got["action"] == "wait"

    def test_green_checks_are_wait(self):
        got = mp.classify(_pr(rollup=[_check("tests", "SUCCESS"),
                                      _check("lint", "SKIPPED")]))
        assert got["action"] == "wait"


class TestReviewHandling:
    def test_changes_requested_is_detected(self):
        got = mp.classify(_pr(comments=[_comment(
            "maintainer", "please add a test for the empty case")]))
        assert got["action"] == "address_review"

    def test_question_is_detected(self):
        got = mp.classify(_pr(comments=[_comment(
            "maintainer", "which version did you reproduce this on?")]))
        assert got["action"] == "answer_question"

    def test_own_comments_do_not_trigger(self):
        """我们自己的回复不该被当成新意见，否则每轮都会重新处理一遍。"""
        got = mp.classify(_pr(comments=[_comment(
            "xixi-box", "please add a test for the empty case")]))
        assert got["action"] == "wait"

    def test_ci_failure_outranks_comment(self):
        """同时有 CI 失败和评论时，先修 CI——评论里问的往往就是 CI 为什么红。"""
        got = mp.classify(_pr(
            comments=[_comment("maintainer", "please fix the failing test")],
            rollup=[_check("tests", "FAILURE")]))
        assert got["action"] == "fix_ci"

    def test_bot_assignment_note(self):
        got = mp.classify(_pr(comments=[_comment(
            "github-actions[bot]", "this issue is already taken by someone")]))
        assert got["action"] == "assignment_note"


class TestRealWorldCases:
    """2026-10-30 实测的几个真实状态。"""

    def test_fastmcp_gate_check_counts_as_ci_failure(self):
        """fastmcp 的 check-issue-link 挂红是常态（等指派），但确实要跟进。"""
        got = mp.classify(_pr(rollup=[_check("check-issue-link", "FAILURE")]))
        assert got["action"] == "fix_ci"

    def test_dependabot_style_noise_is_wait(self):
        """只有 bot 的客套话时不该动手。"""
        got = mp.classify(_pr(comments=[
            _comment("changeset-bot[bot]", "No changeset found in this PR"),
        ]))
        assert got["action"] == "wait"
