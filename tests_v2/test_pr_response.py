"""PR 评论响应链（L1/L2/L3）的单元测试。

L1 = CI 挂红自动派修复（外因过滤 + head_sha 去重）
L2 = 机器人类的机械修复项随单带上（changeset 之类，作为 pr-fix 的上下文）
L3 = AI 草拟回复只落盘，发送必须有显式的 /api/pr/reply/send
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402


def _load_monitor(tmp_path: Path):
    spec = importlib.util.spec_from_file_location(
        "monitor_prs", Path(__file__).resolve().parents[1] / "scripts" / "monitor_prs.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # 去重/草稿文件指到临时目录，别污染真实 state
    mod.ATTEMPTS_FILE = tmp_path / "state" / "pr_fix_attempted.json"
    mod.REPLIES_FILE = tmp_path / "state" / "pr_replies.json"
    return mod


@pytest.fixture()
def mod(tmp_path):
    return _load_monitor(tmp_path)


class TestFailureIsOurs:
    def test_permission_failure_rejected(self, mod):
        failing = [{"name": "pre-run-check",
                    "excerpt": "must have the 'verified' label, or 4+ merged PRs. "
                               "DO NOT request for the label"}]
        ours, why = mod.failure_is_ours(failing)
        assert ours is False
        assert "external" in why

    def test_vercel_auth_rejected(self, mod):
        failing = [{"name": "Vercel – studio",
                    "excerpt": "Authorization required to deploy."}]
        ours, _ = mod.failure_is_ours(failing)
        assert ours is False

    def test_live_llm_flaky_rejected(self, mod):
        failing = [{"name": "test on 3.10",
                    "excerpt": "FAILED tests/realtime/simulation/test_simulation.py::"
                               "test_exploration[gpt-live]"}]
        ours, _ = mod.failure_is_ours(failing)
        assert ours is False

    def test_real_test_failure_accepted(self, mod):
        failing = [{"name": "tests",
                    "excerpt": "FAILED tests/test_div.py::test_int - "
                               "AssertionError: assert 3 == 3.5"}]
        ours, why = mod.failure_is_ours(failing)
        assert ours is True

    def test_no_failures_no_fix(self, mod):
        ours, why = mod.failure_is_ours([])
        assert ours is False


class TestDispatchDedup:
    def test_same_head_only_once(self, mod, monkeypatch):
        monkeypatch.setattr(mod, "pr_head", lambda r, n: {
            "headRefName": "fix/1", "headRefOid": "a" * 40,
            "isCrossRepository": True,
            "headRepositoryOwner": {"login": "xixi-box"}})
        monkeypatch.setattr(mod, "failing_details", lambda r, n: [
            {"name": "tests", "excerpt": "FAILED test_x - AssertionError"}])
        dispatched = []

        def fake_gh(args, timeout=90):
            return ""

        def fake_run_gh(args, timeout=90):
            dispatched.append(args)
            return 0

        monkeypatch.setattr(mod, "_gh", fake_gh)
        monkeypatch.setattr(mod, "run_gh", fake_run_gh)

        r1 = mod.dispatch_pr_fix("acme/real", 7)
        assert r1["dispatched"] is True
        r2 = mod.dispatch_pr_fix("acme/real", 7)
        assert r2["dispatched"] is False and "already" in r2["reason"]
        assert len(dispatched) == 1

    def test_new_head_allows_retry(self, mod, monkeypatch):
        monkeypatch.setattr(mod, "failing_details", lambda r, n: [
            {"name": "tests", "excerpt": "FAILED test_x - AssertionError"}])
        heads = [{"headRefName": "fix/1", "headRefOid": "a" * 40,
                  "isCrossRepository": True,
                  "headRepositoryOwner": {"login": "xixi-box"}},
                 {"headRefName": "fix/1", "headRefOid": "b" * 40,
                  "isCrossRepository": True,
                  "headRepositoryOwner": {"login": "xixi-box"}}]
        it = iter(heads)
        monkeypatch.setattr(mod, "pr_head", lambda r, n: next(it))
        monkeypatch.setattr(mod, "_gh", lambda args, timeout=90: "")
        monkeypatch.setattr(mod, "run_gh", lambda args, timeout=90: 0)

        r1 = mod.dispatch_pr_fix("acme/real", 7)
        r2 = mod.dispatch_pr_fix("acme/real", 7)
        assert r1["dispatched"] is True
        assert r2["dispatched"] is True, "new head sha must allow another attempt"

    def test_not_our_fork_not_dispatched(self, mod, monkeypatch):
        monkeypatch.setattr(mod, "pr_head", lambda r, n: {
            "headRefName": "patch-1", "headRefOid": "c" * 40,
            "isCrossRepository": False,
            "headRepositoryOwner": {"login": "someone"}})
        r = mod.dispatch_pr_fix("acme/real", 7)
        assert r["dispatched"] is False
        assert "fork" in r["reason"]

    def test_external_failure_recorded_not_dispatched(self, mod, monkeypatch):
        monkeypatch.setattr(mod, "pr_head", lambda r, n: {
            "headRefName": "fix/1", "headRefOid": "d" * 40,
            "isCrossRepository": True,
            "headRepositoryOwner": {"login": "xixi-box"}})
        monkeypatch.setattr(mod, "failing_details", lambda r, n: [
            {"name": "check-issue-link", "excerpt": ""}])
        r = mod.dispatch_pr_fix("acme/real", 7)
        assert r["dispatched"] is False
        # 记录在案，同 sha 不再反复尝试
        assert len(mod._attempts()) == 1


class TestPayloadBuild:
    def test_payload_carries_bot_comments(self, mod, monkeypatch):
        """L2：changeset 之类的机械要求要随单带给 agent。"""
        import base64
        captured = {}

        def fake_gh(args, timeout=90):
            if args[:1] == ["api"]:
                return ("changeset-bot[bot]	No Changeset found. Merging this PR "
                        "will not cause a version bump.")
            return ""

        def fake_run_gh(args, timeout=90):
            for c in args:
                if isinstance(c, str) and c.startswith("fix_b64="):
                    captured["payload"] = json.loads(base64.b64decode(c[8:]))
            return 0

        monkeypatch.setattr(mod, "pr_head", lambda r, n: {
            "headRefName": "fix/1", "headRefOid": "e" * 40,
            "isCrossRepository": True,
            "headRepositoryOwner": {"login": "xixi-box"}})
        monkeypatch.setattr(mod, "failing_details", lambda r, n: [
            {"name": "changeset", "excerpt": "no changeset"}])
        monkeypatch.setattr(mod, "_gh", fake_gh)
        monkeypatch.setattr(mod, "run_gh", fake_run_gh)

        mod.dispatch_pr_fix("acme/real", 7)
        assert any("changeset" in c.get("body", "").lower()
                   for c in captured["payload"].get("comments", []))
        assert captured["payload"]["fork_repo"] == "xixi-box/real"


class TestPrFixPrompt:
    def test_prompt_contains_failing_and_rules(self):
        from scout_v2.workers.pr_fix import build_prompt
        p = build_prompt({"failing": [{"name": "tests", "excerpt": "FAILED x"}],
                          "comments": [{"author": "bot", "body": "No Changeset"}]})
        assert "FAILED x" in p
        assert "No Changeset" in p
        assert "Do NOT reformat" in p

    def test_external_only_skips_opencode(self, capsys):
        import subprocess
        import scout_v2.workers.pr_fix as pf
        calls = []
        monkey_unused = None

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            def run(self, model, prompt):
                calls.append(prompt)
                return True, "should not be called"

        orig = pf.OpenCodeClient
        pf.OpenCodeClient = FakeClient
        try:
            tmp = Path(__import__("tempfile").mkdtemp())
            fix = tmp / "fix.json"
            fix.write_text(json.dumps({"failing": [
                {"name": "Vercel – studio",
                 "excerpt": "Authorization required to deploy."}]}),
                encoding="utf-8")
            rc = pf.main.__wrapped__ if hasattr(pf.main, "__wrapped__") else None
            # 直接调 main 的核心路径：构造 argv
            sys.argv = ["pr_fix", "--fix", str(fix), "--repo-dir", str(tmp)]
            pf.main()
        finally:
            pf.OpenCodeClient = orig
        assert calls == []
        out = capsys.readouterr().out
        assert "external-only" in out
