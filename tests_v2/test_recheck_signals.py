"""复检认领信号的三处盲区（2026-11-05 实录修复的回归保护）。

1. 只扫评论不扫 issue 正文——clickhouse/clickhouse#126287：成员 PedroTadim
   在正文里写 "the fix maybe is not trivial to ask AI to do it"，我们没拦。
2. RE_CLAIM 不识别派单（"@groeneai you can fix this"）与禁 AI 模式；维护者
   的问句式派单（"Can you fix this?"）靠 author_association 加权。
3. 竞争 PR body 明写 "Refs #7883" 的直接关联漏检——deer-flow#7883：修复 PR
   #7887 开着 18.5h 后合并，认领时复检放行了。timeline 单页 per_page=100 且
   按最旧在前返回，活跃 issue 的最新 cross-referenced 事件可能不在返回里，
   所以搜索通道按 body 里的 "#<候选号>" 兜底。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scout_v2 import git_host  # noqa: E402


def _fake_gh(issue, comments, timeline, search_items):
    def _read(args, max_retries: int = 2):
        # gh_read 的签名是 (args: list[str])，第一项才是路径
        path = args[0] if isinstance(args, (list, tuple)) and args else str(args)
        if path.startswith("search/issues"):
            return {"items": search_items}
        if "/timeline" in path:
            return timeline
        if "/comments" in path:
            return comments
        if "/issues/" in path:
            return issue
        return None
    return _read


CLEAN_ISSUE = {"number": 7, "title": "crash on empty input",
               "state": "open", "assignees": [], "labels": [],
               "user": {"login": "reporter"}, "author_association": "NONE",
               "body": "Steps to reproduce:\n1. run the CLI\n2. it crashes"}


def _recheck(monkeypatch, issue=CLEAN_ISSUE, comments=(), timeline=(),
             search_items=(), number=7):
    monkeypatch.setattr(git_host, "gh_read",
                        _fake_gh(issue, list(comments), list(timeline),
                                 list(search_items)))
    return git_host.recheck_candidate("acme/real", number,
                                      root_cause_keywords=["crash", "empty"])


class TestBodyScan:
    """修复 1：认领信号必须扫 issue 正文，不只扫评论。"""

    def test_claim_in_body_blocks(self, monkeypatch):
        issue = dict(CLEAN_ISSUE,
                     body="Great catch. I'll submit a PR for this weekend.")
        r = _recheck(monkeypatch, issue=issue)
        assert r.has_claim_signal is True

    def test_clean_body_and_comments_pass(self, monkeypatch):
        r = _recheck(monkeypatch, comments=[
            {"user": {"login": "dev"}, "author_association": "NONE",
             "body": "Confirmed on 2.1, here is the full traceback."}])
        assert r.has_claim_signal is False
        assert r.complete is True

    def test_bot_authored_body_ignored(self, monkeypatch):
        issue = dict(CLEAN_ISSUE, user={"login": "ops-bot"},
                     author_association="NONE",
                     body="I'll work on this automatically.")
        r = _recheck(monkeypatch, issue=issue)
        assert r.has_claim_signal is False

    def test_bot_comment_ignored_regression(self, monkeypatch):
        """bot 评论不算认领信号是原有口径，正文改造后不得回退。"""
        r = _recheck(monkeypatch, comments=[
            {"user": {"login": "claude[bot]"}, "author_association": "NONE",
             "body": "I'll work on this."}])
        assert r.has_claim_signal is False

    def test_clickhouse_anti_ai_body_blocks(self, monkeypatch):
        """2026-11-05 实录原文：正文明说这活别让 AI 干。"""
        issue = dict(CLEAN_ISSUE, author_association="MEMBER",
                     body="Thanks for the report. "
                          "The fix maybe is not trivial to ask AI to do it.")
        r = _recheck(monkeypatch, issue=issue)
        assert r.has_claim_signal is True


class TestDispatchAndAntiAiPatterns:
    """修复 2：RE_CLAIM 增补派单与禁 AI 模式。"""

    def test_reclaim_direct_matches(self):
        m = git_host.RE_CLAIM.search
        assert m("you can fix this")
        assert m("@groeneai you can fix this")
        assert m("You can work on it after the release")
        assert m("@dev can handle the migration")
        assert m("The fix maybe is not trivial to ask AI to do it")
        assert m("Please don't ask AI to implement this")
        assert m("this module is not for AI")
        assert m("keep this repo AI-free")

    def test_reclaim_non_matches(self):
        m = git_host.RE_CLAIM.search
        assert not m("I can fix this")            # 第一人称不在派单模式里
        assert not m("he can fix this")           # 派单只认 you / @user
        assert not m("can you provide a traceback?")
        assert not m("you can take inspiration from the docs")

    def test_dispatch_comment_from_anyone_counts(self, monkeypatch):
        r = _recheck(monkeypatch, comments=[
            {"user": {"login": "random-user"}, "author_association": "NONE",
             "body": "@groeneai you can fix this"}])
        assert r.has_claim_signal is True

    def test_mentor_question_dispatch_weighted(self, monkeypatch):
        r = _recheck(monkeypatch, comments=[
            {"user": {"login": "maintainer"}, "author_association": "MEMBER",
             "body": "Can you fix this?"}])
        assert r.has_claim_signal is True

    def test_same_question_from_random_user_ignored(self, monkeypatch):
        """普通用户的 "Can you fix this?" 更像求助，不加权就不算信号。"""
        r = _recheck(monkeypatch, comments=[
            {"user": {"login": "random-user"}, "author_association": "NONE",
             "body": "Can you fix this?"}])
        assert r.has_claim_signal is False

    def test_mentor_non_dispatch_comment_ignored(self, monkeypatch):
        r = _recheck(monkeypatch, comments=[
            {"user": {"login": "maintainer"}, "author_association": "MEMBER",
             "body": "Thanks for the report, we are looking into it."}])
        assert r.has_claim_signal is False


class TestExplicitRefPr:
    """修复 3：PR 标题/正文明写 "#<候选号>" 的直接关联。"""

    @staticmethod
    def _xref(state="open", merged_at=None, body="", login="someone-else"):
        return [{"event": "cross-referenced",
                 "source": {"issue": {
                     "number": 99, "state": state,
                     "user": {"login": login},
                     "pull_request": {"state": state, "merged_at": merged_at},
                     "body": body}}}]

    def test_open_pr_refs_issue_in_timeline(self, monkeypatch):
        r = _recheck(monkeypatch, timeline=self._xref(body="Refs #7"))
        assert r.has_cross_ref_pr is True

    def test_merged_pr_with_explicit_ref_blocks(self, monkeypatch):
        """deer-flow#7883 的下一幕：合并后 issue 仍 open（Refs 不自动关），
        复检必须拦，否则认领一个已修好的 issue。"""
        r = _recheck(monkeypatch, timeline=self._xref(
            state="closed", merged_at="2026-11-04T12:00:00Z", body="Fixes #7"))
        assert r.has_cross_ref_pr is True

    def test_merged_pr_without_explicit_ref_passes(self, monkeypatch):
        """已合并但没提候选号的引用维持"不算竞争"的原口径。"""
        r = _recheck(monkeypatch, timeline=self._xref(
            state="closed", merged_at="2026-11-04T12:00:00Z",
            body="drive-by refactor of the widget"))
        assert r.has_cross_ref_pr is False

    def test_closed_unmerged_refs_still_pass(self, monkeypatch):
        """弃坑（关闭未合并）不算竞争是既定口径，即使 body 提到候选号。"""
        r = _recheck(monkeypatch, timeline=self._xref(
            state="closed", body="Refs #7"))
        assert r.has_cross_ref_pr is False

    def test_own_merged_pr_not_competition(self, monkeypatch):
        """我们自己的 PR body 总写着 Fixes #N，作者排除必须先于文本检查。"""
        r = _recheck(monkeypatch, timeline=self._xref(
            state="closed", merged_at="2026-11-04T12:00:00Z",
            body="Fixes #7", login="xixi-box"))
        assert r.has_cross_ref_pr is False

    def test_number_boundary_no_false_positive(self, monkeypatch):
        """#17883 / #780 不得命中候选 #7。"""
        r = _recheck(monkeypatch, timeline=self._xref(
            state="closed", merged_at="2026-11-04T12:00:00Z",
            body="related to #17883 and #780"))
        assert r.has_cross_ref_pr is False

    def test_search_body_refs_issue_hits(self, monkeypatch):
        """timeline 截断（活跃 issue 事件超过单页 100 条、最旧在前）时的
        兜底：搜索响应必带全量 body，明写 "#<候选号>" 即判竞争。"""
        r = _recheck(monkeypatch, search_items=[
            {"number": 88, "title": "polish input widget",
             "user": {"login": "someone-else"}, "body": "Refs #7"}])
        assert r.open_pr_hits, "explicit body reference must be caught"

    def test_search_without_ref_or_keywords_passes(self, monkeypatch):
        r = _recheck(monkeypatch, search_items=[
            {"number": 88, "title": "polish input widget",
             "user": {"login": "someone-else"}, "body": "unrelated"}])
        assert r.open_pr_hits == []

    def test_search_ref_number_boundary(self, monkeypatch):
        r = _recheck(monkeypatch, search_items=[
            {"number": 88, "title": "polish input widget",
             "user": {"login": "someone-else"}, "body": "Refs #17883"}])
        assert r.open_pr_hits == []

    def test_deerflow_scenario_end_to_end(self, monkeypatch):
        """完整实录：issue 干净无认领，竞争 PR 标题无关键词交集、
        body 写 "Refs #7"——修复前两路全漏，修复后必须 abort。"""
        r = _recheck(monkeypatch, search_items=[
            {"number": 88, "title": "polish input widget",
             "user": {"login": "someone-else"}, "body": "Refs #7"}])
        assert r.complete is True
        assert r.issue_open and r.unassigned and not r.has_claim_signal
        assert r.open_pr_hits or r.has_cross_ref_pr
