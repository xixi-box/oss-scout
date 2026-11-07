"""PR 监控分类的单元测试。

PR 开出去之后没人管是最容易烂尾的地方。分类逻辑决定"要不要动手"，判错的
代价是双向的：漏判＝PR 烂掉，误判＝对着机器人的客套话瞎忙一轮。
"""
from __future__ import annotations

import json
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


class TestReviewDispatch:
    """维护者提修改要求 → 派 opencode 落实到 PR 分支。

    与 CI 修复同一条流水线，但去重键不同：CI 按 head_sha（推新 commit 才是
    新一轮），review 按评论内容（同一条要求只落实一次，否则会反复重做）。
    """

    HEAD = {"isCrossRepository": True, "state": "OPEN",
            "author": {"login": "xixi-box"}, "headRepository": {"name": "compose"},
            "headRepositoryOwner": {"login": "xixi-box"},
            "headRefName": "fix/issue-1-abc", "headRefOid": "a" * 40}

    def _setup(self, monkeypatch, tmp_path, *, tracked=False):
        monkeypatch.setattr(mp, "pr_head", lambda r, n: dict(self.HEAD))
        monkeypatch.setattr(mp, "ATTEMPTS_FILE", tmp_path / "attempts.json")
        monkeypatch.setattr(mp, "_gh", lambda *a, **k: "")
        monkeypatch.setattr(mp, "failing_details", lambda r, n: [])
        monkeypatch.setattr(mp, "failure_is_ours",
                            lambda f: (False, "no failures"))
        texts = ("please add a regression test", "please add a test", "add a test", "also update the changelog")
        monkeypatch.setattr(mp, "_thread", lambda *a: [
            {"author": "maint" if i < 2 else "m", "association": "MEMBER",
             "body": body, "source": "issue", "comment_id": i} for i, body in enumerate(texts)
        ])
        runs = []
        monkeypatch.setattr(mp, "run_gh",
                            lambda args, timeout=90: runs.append(args) or 0)
        return runs

    def test_review_kind_dispatches_despite_no_ci_failure(
            self, monkeypatch, tmp_path):
        """CI 全绿但维护者要求改代码时，仍然要派活。"""
        runs = self._setup(monkeypatch, tmp_path)
        out = mp.dispatch_pr_fix("docker/compose", 7, kind="review",
                                 review_comment="please add a regression test",
                                 review_author="maint")
        assert out["dispatched"] is True
        assert out["kind"] == "review"
        assert runs, "expected a workflow dispatch"

    def test_same_review_is_not_dispatched_twice(self, monkeypatch, tmp_path):
        self._setup(monkeypatch, tmp_path)
        kw = dict(kind="review", review_comment="please add a test",
                  review_author="maint")
        assert mp.dispatch_pr_fix("docker/compose", 7, **kw)["dispatched"] is True
        second = mp.dispatch_pr_fix("docker/compose", 7, **kw)
        assert second["dispatched"] is False
        assert "already attempted" in second["reason"]

    def test_different_review_dispatches_again(self, monkeypatch, tmp_path):
        self._setup(monkeypatch, tmp_path)
        mp.dispatch_pr_fix("docker/compose", 7, kind="review",
                           review_comment="add a test", review_author="m")
        out = mp.dispatch_pr_fix("docker/compose", 7, kind="review",
                                 review_comment="also update the changelog",
                                 review_author="m")
        assert out["dispatched"] is True

    def test_empty_review_is_rejected(self, monkeypatch, tmp_path):
        self._setup(monkeypatch, tmp_path)
        out = mp.dispatch_pr_fix("docker/compose", 7, kind="review",
                                 review_comment="  ", review_author="m")
        assert out["dispatched"] is False

    def test_ci_kind_still_skips_external_failures(self, monkeypatch, tmp_path):
        """泛化不能把 CI 那条路径的外因过滤弄丢。"""
        self._setup(monkeypatch, tmp_path)
        monkeypatch.setattr(mp, "failure_is_ours",
                            lambda f: (False, "external check by name"))
        out = mp.dispatch_pr_fix("docker/compose", 7, kind="ci")
        assert out["dispatched"] is False
        assert "external" in out["reason"]

    def test_non_fork_head_is_never_dispatched(self, monkeypatch, tmp_path):
        self._setup(monkeypatch, tmp_path)
        monkeypatch.setattr(mp, "pr_head", lambda r, n: {
            "isCrossRepository": False,
            "headRepositoryOwner": {"login": "docker"},
            "headRefName": "x", "headRefOid": "b" * 40})
        out = mp.dispatch_pr_fix("docker/compose", 7, kind="review",
                                 review_comment="change it")
        assert out["dispatched"] is False
        assert "fork" in out["reason"]


class TestReviewPrompt:
    """review 类任务的 prompt 必须写明"改什么"，而不是让模型猜。"""

    def test_prompt_carries_the_review_verbatim(self):
        from scout_v2.workers import pr_fix as pf
        p = pf.build_prompt({"kind": "review", "review_author": "ndeloof",
                             "review_comment": "update the failing test to expect "
                                              "[\"/repo/*\", \"!/repo/file.txt\"]"})
        assert "ndeloof" in p
        assert "!/repo/file.txt" in p
        assert "regression test" in p.lower()

    def test_ci_prompt_unchanged_for_ci_payloads(self):
        from scout_v2.workers import pr_fix as pf
        p = pf.build_prompt({"kind": "ci",
                             "failing": [{"name": "tests", "excerpt": "FAILED x"}]})
        assert "CI is failing" in p
        assert "tests" in p


class TestReplyTargetIsExternal:
    """起草的对象必须是别人的最新发言，不能是我们自己的话。

    2026-11-04 实录：回复发出后 thread[-1] 是我们自己的评论，下一轮
    draft_reply 会对着自己的话再起草一遍，卡片把我们的措辞标成
    "对方评论（ndeloof）"。
    """

    def test_draft_targets_last_external_comment(self, monkeypatch, tmp_path):
        monkeypatch.setattr(mp, "REPLIES_FILE", tmp_path / "replies.json")
        monkeypatch.setattr(mp, "_thread", lambda r, n, limit=10: [
            {"created_at": "2026-11-04T08:52:08Z", "author": "ndeloof",
             "body": "please also add a regression test at the Matches level"},
            {"created_at": "2026-11-04T11:02:04Z", "author": "xixi-box",
             "body": "Updated the test and added the regression case."},
        ])
        monkeypatch.setattr(mp, "_model", lambda: ("", ""))
        out = mp.draft_reply("docker/compose", 14450, force=True)
        # 无模型 key 时会失败，但失败原因必须建立在"对方评论"上：
        # 落盘的 for_comment/作者都还没写，所以只看 reason 不含 own-words 误判
        assert out["drafted"] is False
        assert "no model" in out["reason"]

    def test_only_our_comments_means_no_draft(self, monkeypatch, tmp_path):
        monkeypatch.setattr(mp, "REPLIES_FILE", tmp_path / "replies.json")
        monkeypatch.setattr(mp, "_thread", lambda r, n, limit=10: [
            {"created_at": "2026-11-04T11:02:04Z", "author": "xixi-box",
             "body": "our own reply"}])
        out = mp.draft_reply("docker/compose", 14450)
        assert out["drafted"] is False
        assert out["reason"] == "only our own comments"

    def test_latest_external_comment_skips_ours(self, monkeypatch):
        monkeypatch.setattr(mp, "_thread", lambda r, n, limit=30: [
            {"created_at": "t1", "author": "ndeloof", "body": "review"},
            {"created_at": "t2", "author": "xixi-box", "body": "our reply"},
            {"created_at": "t3", "author": "someone-else", "body": "me too"},
            {"created_at": "t4", "author": "xixi-box", "body": "our reply 2"},
        ])
        got = mp.latest_external_comment("docker/compose", 14450)
        assert got["author"] == "someone-else"
        assert got["body"] == "me too"

    def test_latest_external_comment_none_when_all_ours(self, monkeypatch):
        monkeypatch.setattr(mp, "_thread", lambda r, n, limit=30: [
            {"created_at": "t1", "author": "xixi-box", "body": "ours"}])
        assert mp.latest_external_comment("docker/compose", 14450) is None


class TestInlineComments:
    """行内 review 评论必须进入分类和起草视野。

    agentgateway#4264 实录：维护者把"改成 cfg 条件编译"写在行内，
    issues/{n}/comments 不含行内评论——分类器看不见，一条必须改代码的
    review 被静默漏掉。
    """

    def test_thread_includes_inline_comments(self, monkeypatch):
        calls = []

        def fake_gh(args, timeout=90):
            calls.append(args[1])
            if "/pulls/" in args[1]:
                return json.dumps([[{"created_at": "2026-11-04T14:09:43Z", "id": 12345,
                    "user": {"login": "keithmattix"}, "body": "use cfg instead of passing windows"}]])
            return json.dumps([[{"created_at": "2026-11-02T17:15:23Z", "id": 987,
                "user": {"login": "someone"}, "body": "first comment"}]])

        monkeypatch.setattr(mp, "_gh", fake_gh)
        thread = mp._thread("agentgateway/agentgateway", 4264)
        inline = [c for c in thread if c["source"] == "inline"]
        issue = [c for c in thread if c["source"] == "issue"]
        assert inline and inline[0]["author"] == "keithmattix"
        assert inline[0]["comment_id"] == 12345
        assert issue and issue[0]["comment_id"] == 987
        # 两个端点都被拉了
        assert any("/pulls/" in c for c in calls)
        assert any("/issues/" in c for c in calls)
        # 按时间排序
        times = [c["created_at"] for c in thread]
        assert times == sorted(times)

    def test_pr_state_merges_inline_comments(self, monkeypatch):
        def fake_gh(args, timeout=90):
            joined = " ".join(args)
            if "/pulls/" in joined:
                return ("2026-11-04T14:09:43Z|keithmattix|12345|"
                        "please use cfg instead of the flag")
            return json.dumps({
                "comments": [{"author": {"login": "old"}, "body": "hi",
                              "createdAt": "2026-11-02T00:00:00Z"}],
                "statusCheckRollup": [], "reviewDecision": "",
                "isDraft": False, "title": "t", "body": "", "url": ""})

        monkeypatch.setattr(mp, "_gh", fake_gh)
        state = mp.pr_state("a/b", 1)
        bodies = [c.get("body", "") for c in state["comments"]]
        assert any("cfg" in b for b in bodies), state["comments"]
        # 行内评论按时间排进同一条时间线
        times = [c.get("createdAt", "") for c in state["comments"]]
        assert times == sorted(times)


class TestAlreadyRepliedGate:
    """球在对面时不再起草——刷屏的根源就在这。"""

    def _thread(self):
        return [
            {"created_at": "2026-11-04T08:52:08Z", "author": "ndeloof",
             "body": "please do X"},
            {"created_at": "2026-11-04T11:02:04Z", "author": "xixi-box",
             "body": "Done in abcdef."},
        ]

    def test_reply_newer_than_external_skips(self, monkeypatch, tmp_path):
        (tmp_path / "state").mkdir()
        monkeypatch.setattr(mp, "REPLIES_FILE",
                            tmp_path / "state" / "pr_replies.json")
        monkeypatch.setattr(mp, "_thread", lambda r, n, limit=10: self._thread())
        out = mp.draft_reply("docker/compose", 14450)
        assert out["drafted"] is False
        assert out["reason"] == "already replied; waiting on them"

    def test_new_external_comment_reopens_drafting(self, monkeypatch, tmp_path):
        (tmp_path / "state").mkdir()
        monkeypatch.setattr(mp, "REPLIES_FILE",
                            tmp_path / "state" / "pr_replies.json")
        thread = self._thread() + [
            {"created_at": "2026-11-04T14:09:43Z", "author": "ndeloof",
             "body": "one more thing"}]
        monkeypatch.setattr(mp, "_thread", lambda r, n, limit=10: thread)
        monkeypatch.setattr(mp, "_model", lambda: ("", ""))
        out = mp.draft_reply("docker/compose", 14450)
        # 对方又说话了 → 应该尝试起草（无 key 时报 no model key）
        assert out["reason"] == "no model key"

    def test_force_overrides_gate(self, monkeypatch, tmp_path):
        (tmp_path / "state").mkdir()
        monkeypatch.setattr(mp, "REPLIES_FILE",
                            tmp_path / "state" / "pr_replies.json")
        monkeypatch.setattr(mp, "_thread", lambda r, n, limit=10: self._thread())
        monkeypatch.setattr(mp, "_model", lambda: ("", ""))
        out = mp.draft_reply("docker/compose", 14450, force=True)
        assert out["reason"] == "no model key"   # 走到了模型那步


class TestReplyTargetRouting:
    """行内发言行内回、行外发言行外回——发送端按草稿来源选端点。"""

    def test_draft_records_inline_source(self, monkeypatch, tmp_path):
        replies = tmp_path / "state" / "pr_replies.json"
        replies.parent.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(mp, "REPLIES_FILE", replies)
        monkeypatch.setattr(mp, "_thread", lambda r, n, limit=10: [
            {"created_at": "2026-11-04T14:09:43Z", "author": "keithmattix",
             "comment_id": 12345, "source": "inline", "body": "use cfg"},
        ])
        monkeypatch.setattr(mp, "_model", lambda: ("k", "m"))

        class Fake:
            def __init__(self, **k):
                pass

            def chat(self, prompt, max_tokens=200):
                return True, "Sure, switching to cfg."

        monkeypatch.setattr("scout_v2.workers.issue_scorer.GoClient", Fake)
        out = mp.draft_reply("agentgateway/agentgateway", 4264, force=True)
        assert out["drafted"] is True
        entry = json.loads(replies.read_text(encoding="utf-8")
                           )["agentgateway/agentgateway#4264"]
        assert entry["source_kind"] == "inline"
        assert entry["source_comment_id"] == 12345

    def test_draft_records_issue_source(self, monkeypatch, tmp_path):
        replies = tmp_path / "state" / "pr_replies.json"
        replies.parent.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(mp, "REPLIES_FILE", replies)
        monkeypatch.setattr(mp, "_thread", lambda r, n, limit=10: [
            {"created_at": "2026-11-02T17:15:23Z", "author": "ndeloof",
             "comment_id": 777, "source": "issue", "body": "please do X"},
        ])
        monkeypatch.setattr(mp, "_model", lambda: ("k", "m"))

        class Fake:
            def __init__(self, **k):
                pass

            def chat(self, prompt, max_tokens=200):
                return True, "Done."

        monkeypatch.setattr("scout_v2.workers.issue_scorer.GoClient", Fake)
        mp.draft_reply("docker/compose", 1, force=True)
        entry = json.loads(replies.read_text(encoding="utf-8")
                           )["docker/compose#1"]
        assert entry["source_kind"] == "issue"
        assert entry["source_comment_id"] == 777


def test_untrusted_review_does_not_dispatch(monkeypatch, tmp_path):
    runs = TestReviewDispatch()._setup(monkeypatch, tmp_path)
    monkeypatch.setattr(mp, "_thread", lambda *a: [
        {"author": "visitor", "association": "NONE", "body": "please change this",
         "source": "issue", "comment_id": 99},
    ])
    out = mp.dispatch_pr_fix("docker/compose", 7, kind="review",
                             review_author="visitor", review_comment="please change this")
    assert not out["dispatched"]
    assert not runs


def test_company_pr_rejected_before_github_read(monkeypatch):
    def unexpected(*args):
        pytest.fail("out-of-roster PR must not be read")
    monkeypatch.setattr(mp, "pr_head", unexpected)
    assert not mp.dispatch_pr_fix("deep-optica/example", 7)["dispatched"]
