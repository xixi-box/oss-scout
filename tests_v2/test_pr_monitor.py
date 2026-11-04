"""PR 监控分类的单元测试。

PR 开出去之后没人管是最容易烂尾的地方。分类逻辑决定"要不要动手"，判错的
代价是双向的：漏判＝PR 烂掉，误判＝对着机器人的客套话瞎忙一轮。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import importlib.util  # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    "monitor_prs", Path(__file__).resolve().parents[1] / "scripts" / "monitor_prs.py")
mp = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mp)


@pytest.fixture(autouse=True)
def _no_model(monkeypatch):
    """默认关掉模型调用：单测不该联网，也不该因为模型抖动而随机红。

    评论分类现在是 AI 优先、规则兜底；这里强制走兜底路径，规则行为照测。
    AI 分支在 TestCommentClassification 里单独打桩。
    """
    monkeypatch.setattr(mp, "_model", lambda: ("", ""))


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


class TestPleasantry:
    """催 review / 打招呼不该被当成 review 去起草技术回复。

    2026-11-04 实录（software-agent-sdk#6269）：「All green here, rebased,
    could someone review?」被起草成一封解释修复细节的信——对方只是想知道
    什么时候有人看，AI 自说自话反而露怯。
    """

    def test_review_ping_is_wait(self):
        got = mp.classify(_pr(comments=[_comment(
            "contributor",
            "Hey team! @OpenHands/maintainers All green here, no conflicts, "
            "rebased on latest main. Could someone review when you get a "
            "chance? Thanks!")]))
        assert got["action"] == "wait"

    def test_any_update_is_wait(self):
        got = mp.classify(_pr(comments=[_comment(
            "contributor", "any update on this?")]))
        assert got["action"] == "wait"

    def test_lgtm_is_wait(self):
        got = mp.classify(_pr(comments=[_comment("maintainer", "LGTM!")]))
        assert got["action"] == "wait"

    def test_ping_with_real_ask_is_still_review(self):
        """客套句里夹了真诉求，必须照 review 处理（宁可多起草一次）。"""
        got = mp.classify(_pr(comments=[_comment(
            "maintainer",
            "All green here, but why does this swallow the exception?")]))
        assert got["action"] in ("address_review", "answer_question")

    def test_thanks_with_request_is_still_review(self):
        got = mp.classify(_pr(comments=[_comment(
            "maintainer", "Thanks! Could you also add a test for the empty case?")]))
        assert got["action"] == "address_review"

    def test_mentioned_org_does_not_break_detection(self):
        """@org/team 里的斜杠曾把句式正则打断，导致客套判不出来。"""
        assert mp.is_pleasantry("Hey @OpenHands/maintainers all green, "
                                "could someone review?")
        # 加了诉求词就不再是客套（please 是真的在要求做事）
        assert not mp.is_pleasantry("Hey @OpenHands/maintainers bump please")


class TestCommentClassification:
    """评论分类：模型判断意图，规则只兜底。

    2026-11-04 实录：分类器此前是纯正则，「All green, could someone review?」
    和「All green, but the test fails」只差一个词，靠正则区分不了。
    """

    def test_model_verdict_is_used(self, monkeypatch):
        monkeypatch.setattr(mp, "_model", lambda: ("k", "m"))

        class Fake:
            def __init__(self, **k):
                pass

            def chat(self, prompt, max_tokens=200):
                return True, "ACTION: wait\nREASON: review ping, no ask"

        monkeypatch.setattr(
            "scout_v2.workers.issue_scorer.GoClient", Fake)
        got = mp.classify_comment("All green, could someone review?")
        assert got["ok"] is True
        assert got["action"] == "wait"
        assert "ping" in got["reason"]

    def test_unparseable_output_falls_back(self, monkeypatch):
        monkeypatch.setattr(mp, "_model", lambda: ("k", "m"))

        class Fake:
            def __init__(self, **k):
                pass

            def chat(self, prompt, max_tokens=200):
                return True, "I think this is probably fine?"

        monkeypatch.setattr("scout_v2.workers.issue_scorer.GoClient", Fake)
        got = mp.classify_comment("something")
        assert got["ok"] is False

    def test_model_error_falls_back(self, monkeypatch):
        monkeypatch.setattr(mp, "_model", lambda: ("k", "m"))

        class Fake:
            def __init__(self, **k):
                pass

            def chat(self, prompt, max_tokens=200):
                return False, "HTTP 503"

        monkeypatch.setattr("scout_v2.workers.issue_scorer.GoClient", Fake)
        got = mp.classify_comment("something")
        assert got["ok"] is False

    def test_no_key_falls_back(self):
        got = mp.classify_comment("anything")   # autouse fixture 无 key
        assert got["ok"] is False

    def test_model_unavailable_never_silently_waits(self, monkeypatch):
        """模型挂了必须退回规则，不能把"模型不可用"当成"不用管"。"""
        got = mp.classify(_pr(comments=[_comment(
            "maintainer", "please add a test for the empty case")]))
        assert got["action"] == "address_review"

    def test_model_verdict_drives_action(self, monkeypatch):
        monkeypatch.setattr(mp, "_model", lambda: ("k", "m"))

        class Fake:
            def __init__(self, **k):
                pass

            def chat(self, prompt, max_tokens=200):
                return True, "ACTION: answer_question\nREASON: asks why the exception is swallowed"

        monkeypatch.setattr("scout_v2.workers.issue_scorer.GoClient", Fake)
        got = mp.classify(_pr(comments=[_comment(
            "maintainer", "All green here, but why does this swallow the exception?")]))
        assert got["action"] == "answer_question"

    def test_verdict_is_cached_per_comment(self, monkeypatch, tmp_path):
        """同一轮 15 分钟扫描里不该重复调用模型。"""
        calls = []

        class Fake:
            def __init__(self, **k):
                pass

            def chat(self, prompt, max_tokens=200):
                calls.append(prompt)
                return True, "ACTION: wait\nREASON: ping"

        monkeypatch.setattr(mp, "_model", lambda: ("k", "m"))
        monkeypatch.setattr("scout_v2.workers.issue_scorer.GoClient", Fake)
        monkeypatch.setattr(mp, "CLASSIFY_CACHE", tmp_path / "cache.json")
        monkeypatch.setattr(mp, "_cache_load", lambda: _load())
        monkeypatch.setattr(mp, "_cache_put", lambda k, v: _put(k, v))
        store: dict = {}
        _load, _put = (lambda: dict(store)), (lambda k, v: store.__setitem__(k, v))

        pr = _pr(comments=[{"author": {"login": "maint"},
                            "body": "all green, could someone review?",
                            "createdAt": "2026-11-04T01:00:00Z"}],
                 repo="a/b", number=1, title="t")

        mp.classify(pr)
        mp.classify(pr)          # 第二轮：同一条评论
        assert len(calls) == 1, f"expected 1 model call, got {len(calls)}"

    def test_new_comment_bypasses_cache(self, monkeypatch):
        """评论更新后必须重新判定，不能被旧结论挡住。"""
        calls = []

        class Fake:
            def __init__(self, **k):
                pass

            def chat(self, prompt, max_tokens=200):
                calls.append(prompt)
                return True, "ACTION: wait\nREASON: ping"

        monkeypatch.setattr(mp, "_model", lambda: ("k", "m"))
        monkeypatch.setattr("scout_v2.workers.issue_scorer.GoClient", Fake)
        store: dict = {}
        monkeypatch.setattr(mp, "_cache_load", lambda: dict(store))
        monkeypatch.setattr(mp, "_cache_put",
                            lambda k, v: store.__setitem__(k, v))

        base = {"repo": "a/b", "number": 1, "title": "t", "statusCheckRollup": [],
                "reviewDecision": "", "isDraft": False}
        mp.classify({**base, "comments": [
            {"author": {"login": "m"}, "body": "ping",
             "createdAt": "2026-11-04T01:00:00Z"}]})
        mp.classify({**base, "comments": [
            {"author": {"login": "m"}, "body": "ping again",
             "createdAt": "2026-11-04T02:00:00Z"}]})
        assert len(calls) == 2
