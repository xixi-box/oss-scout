"""被判重复的 issue 不碰。

2026-11-03 实录（qdrant#11361）：我们修对了 bug，但关联的 #11360 早已被判成
#10465 的 duplicate，而 #10465 已有 #10470 在推进。PR 开出去几小时后被
qdrant-cloud-bot 关掉：
    "Declined — linked issue #11360 was closed as a duplicate of #10465.
     Prefer continuing there / in #10470 rather than merging this parallel fix."

复检此前只查"是否 open / 有无 assignee / 有无竞争 PR"，不查"这条 issue 本身
是不是重复"。被判重复与关闭之间有时间窗，我们正好撞进那个窗口。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scout_v2 import git_host  # noqa: E402


def _fake_gh(issue, comments=None, timeline=None, search=None):
    def _read(args, max_retries=2):
        path = args[0] if isinstance(args, (list, tuple)) and args else str(args)
        if path.startswith("search/issues"):
            return {"items": search or []}
        if "/timeline" in path:
            return timeline or []
        if "/comments" in path:
            return comments or []
        if "/issues/" in path:
            return issue
        return None
    return _read


BASE = {"number": 7, "state": "open", "assignees": [], "labels": [],
        "body": "something is broken"}


class TestDuplicateDetection:
    def test_state_reason_duplicate(self, monkeypatch):
        issue = {**BASE, "state_reason": "duplicate"}
        monkeypatch.setattr(git_host, "gh_read", _fake_gh(issue))
        r = git_host.recheck_candidate("acme/real", 7)
        assert r.is_duplicate is True

    def test_body_says_duplicate_of(self, monkeypatch):
        issue = {**BASE, "body": "This is a duplicate of #10465, same root cause."}
        monkeypatch.setattr(git_host, "gh_read", _fake_gh(issue))
        r = git_host.recheck_candidate("acme/real", 7)
        assert r.is_duplicate is True
        assert r.duplicate_of == 10465

    def test_dupe_of_spelling(self, monkeypatch):
        issue = {**BASE, "body": "dupe of #42"}
        monkeypatch.setattr(git_host, "gh_read", _fake_gh(issue))
        r = git_host.recheck_candidate("acme/real", 7)
        assert r.is_duplicate is True and r.duplicate_of == 42

    def test_same_as_spelling(self, monkeypatch):
        issue = {**BASE, "body": "same as #99"}
        monkeypatch.setattr(git_host, "gh_read", _fake_gh(issue))
        r = git_host.recheck_candidate("acme/real", 7)
        assert r.duplicate_of == 99

    def test_negation_is_not_flagged(self, monkeypatch):
        """‘这不是 #123 的重复’不该被判成重复——开发者在排除重复，不是声明重复。"""
        issue = {**BASE, "body": "This is not a duplicate of #123; different crash."}
        monkeypatch.setattr(git_host, "gh_read", _fake_gh(issue))
        r = git_host.recheck_candidate("acme/real", 7)
        assert r.is_duplicate is False

    def test_no_longer_duplicate(self, monkeypatch):
        issue = {**BASE, "body": "no longer a dupe of #55, upstream changed"}
        monkeypatch.setattr(git_host, "gh_read", _fake_gh(issue))
        assert git_host.recheck_candidate("acme/real", 7).is_duplicate is False

    def test_ordinary_issue_not_duplicate(self, monkeypatch):
        issue = {**BASE, "state_reason": "completed"}
        monkeypatch.setattr(git_host, "gh_read", _fake_gh(issue))
        r = git_host.recheck_candidate("acme/real", 7)
        assert r.is_duplicate is False
        assert r.duplicate_of is None

    def test_duplicate_detected_even_when_open(self, monkeypatch):
        """关键：被判重复的 issue 可能仍然 open——那正是我们撞进去的窗口。"""
        issue = {**BASE, "state": "open", "state_reason": "duplicate"}
        monkeypatch.setattr(git_host, "gh_read", _fake_gh(issue))
        r = git_host.recheck_candidate("acme/real", 7)
        assert r.issue_open is True
        assert r.is_duplicate is True
