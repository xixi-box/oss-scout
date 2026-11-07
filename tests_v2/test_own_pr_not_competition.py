"""复检竞争判定必须排除我们自己的 PR。

2026-11-02 实录（最贵的 bug）：修复成功开出 PR 后，下一轮 drain 的复检用
「关键词搜 open PR」和「timeline cross-reference」判断竞争，两者都命中了
**我们刚开出的那个 PR**，候选被判 aborted_competed；abort 路径又归还了
claim 额度，于是同一天连续开出 5 个 PR（配置上限 2），并且这 5 个 PR 对应
的候选全不在 pr_open 里——pr_open 只有 11 个，实际 open PR 35 个。
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


BASE_ISSUE = {"number": 7, "title": "crash on empty input",
              "state": "open", "assignees": [], "labels": []}


class TestOwnPrIsNotCompetition:
    def test_own_open_pr_in_search_ignored(self, monkeypatch):
        monkeypatch.setattr(git_host, "gh_read", _fake_gh(
            BASE_ISSUE, [], [],
            [{"number": 99, "title": "fix: crash on empty input",
              "user": {"login": "xixi-box"}}]))
        r = git_host.recheck_candidate("acme/real", 7,
                                       root_cause_keywords=["crash", "empty input"])
        assert r.open_pr_hits == [], "our own PR must not count as a competitor"
        assert r.has_cross_ref_pr is False

    def test_other_open_pr_still_competition(self, monkeypatch):
        monkeypatch.setattr(git_host, "gh_read", _fake_gh(
            BASE_ISSUE, [], [],
            [{"number": 88, "title": "fix: crash on empty input",
              "user": {"login": "someone-else"}, "body": "Fixes #7"}]))
        r = git_host.recheck_candidate("acme/real", 7,
                                       root_cause_keywords=["crash", "empty input"])
        assert r.open_pr_hits, "a real competitor must still be detected"

    def test_own_cross_ref_ignored(self, monkeypatch):
        timeline = [{"event": "cross-referenced",
                     "source": {"issue": {
                         "number": 99, "state": "open",
                         "user": {"login": "xixi-box"},
                         "pull_request": {"state": "open"}}}}]
        monkeypatch.setattr(git_host, "gh_read", _fake_gh(
            BASE_ISSUE, [], timeline, []))
        r = git_host.recheck_candidate("acme/real", 7,
                                       root_cause_keywords=["crash"])
        assert r.has_cross_ref_pr is False

    def test_other_cross_ref_still_competition(self, monkeypatch):
        timeline = [{"event": "cross-referenced",
                     "source": {"issue": {
                         "number": 99, "state": "open",
                         "user": {"login": "someone-else"}, "body": "Fixes #7",
                         "pull_request": {"state": "open"}}}}]
        monkeypatch.setattr(git_host, "gh_read", _fake_gh(
            BASE_ISSUE, [], timeline, []))
        r = git_host.recheck_candidate("acme/real", 7,
                                       root_cause_keywords=["crash"])
        assert r.has_cross_ref_pr is True

    def test_closed_own_pr_still_excluded(self, monkeypatch):
        """已关闭的 PR 本来就不算竞争（与作者无关），回归保护。"""
        timeline = [{"event": "cross-referenced",
                     "source": {"issue": {
                         "number": 99, "state": "closed",
                         "user": {"login": "xixi-box"},
                         "pull_request": {"state": "closed"}}}}]
        monkeypatch.setattr(git_host, "gh_read", _fake_gh(
            BASE_ISSUE, [], timeline, []))
        r = git_host.recheck_candidate("acme/real", 7,
                                       root_cause_keywords=["crash"])
        assert r.has_cross_ref_pr is False
