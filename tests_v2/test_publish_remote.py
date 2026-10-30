"""runner 侧发布（2026-10-28 架构调整）的单元测试。

写操作从 VPS 移到 Actions runner：VPS 到 github.com 只有一条代理线路，
断几分钟就让已验证的修复作废。runner 有稳定出网与 PAT，写在那里没有单点。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import subprocess  # noqa: E402

from scout_v2.workers import publish_remote as pr  # noqa: E402


def _ok(args, cwd=None):
    return subprocess.CompletedProcess(args, 0, "", "")


class TestPublishGuards:
    def test_incomplete_config_refused(self, tmp_path):
        patch = tmp_path / "change.patch"
        patch.write_text("--- a/x\n+++ b/x\n", encoding="utf-8")
        r = pr.publish(tmp_path, patch, {"push_repo": "me/fork"})
        assert r["ok"] is False
        assert "incomplete" in r["error"]

    def test_missing_patch_refused(self, tmp_path):
        r = pr.publish(tmp_path, tmp_path / "nope.patch",
                       {"push_repo": "me/fork", "upstream": "up/stream",
                        "branch": "fix/x", "title": "fix: x"})
        assert r["ok"] is False
        assert "no patch" in r["error"]

    def test_empty_publish_config_means_no_publish(self, tmp_path):
        """payload 不带 publish 时，workflow 不该写任何东西。"""
        assert pr.publish_from_env({"publish": {}}, tmp_path, tmp_path) is None
        assert pr.publish_from_env({}, tmp_path, tmp_path) is None


class TestPublishFlow:
    def test_success_returns_pr_url(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        patch = tmp_path / "change.patch"
        patch.write_text("--- a/x\n+++ b/x\n", encoding="utf-8")
        calls: list[list[str]] = []

        def fake(args, cwd=None, **kw):
            calls.append(list(args))
            if "--jq" in args and ".default_branch" in args:
                return subprocess.CompletedProcess(args, 0, "main", "")
            if args[:2] == ["gh", "pr"]:
                return subprocess.CompletedProcess(args, 0, "https://github.com/up/stream/pull/7\n", "")
            return subprocess.CompletedProcess(args, 0, "", "")

        monkeypatch.setattr(pr, "_run", fake)
        monkeypatch.setattr(pr.subprocess, "run", fake)
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_X")
        out = pr.publish(repo, patch, {
            "push_repo": "me/fork", "upstream": "up/stream",
            "branch": "fix/1-abc1234", "title": "fix: thing", "body": "b"})
        assert out["ok"] is True
        assert out["pr"] == "https://github.com/up/stream/pull/7"
        push = next(c for c in calls if "push" in c)
        # 推送目标是 fork，不是上游
        assert any("me/fork.git" in a for a in push)
        assert any("refs/heads/fix/1-abc1234" in a for a in push)
        gh = next(c for c in calls if c[:3] == ["gh", "pr", "create"])
        assert gh[gh.index("--repo") + 1] == "up/stream"
        assert gh[gh.index("--head") + 1] == "me:fix/1-abc1234"

    def test_push_failure_redacts_token(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        patch = tmp_path / "change.patch"
        patch.write_text("--- a/x\n+++ b/x\n", encoding="utf-8")

        def fake(args, cwd=None, **kw):
            if "--jq" in args and ".default_branch" in args:
                return subprocess.CompletedProcess(args, 0, "main", "")
            if "push" in args:
                return subprocess.CompletedProcess(
                    args, 1, "", "fatal: could not push https://x-access-token:ghp_LEAK@github.com")
            return subprocess.CompletedProcess(args, 0, "", "")

        monkeypatch.setattr(pr, "_run", fake)
        monkeypatch.setattr(pr.subprocess, "run", fake)
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_LEAK")
        out = pr.publish(repo, patch, {
            "push_repo": "me/fork", "upstream": "up/stream",
            "branch": "fix/2-b", "title": "fix: y"})
        assert out["ok"] is False
        assert "ghp_LEAK" not in out["error"]
        assert "***" in out["error"]

    def test_existing_pr_is_not_a_failure(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        patch = tmp_path / "change.patch"
        patch.write_text("--- a/x\n+++ b/x\n", encoding="utf-8")

        def fake(args, cwd=None, **kw):
            if "--jq" in args and ".default_branch" in args:
                return subprocess.CompletedProcess(args, 0, "main", "")
            if args[:3] == ["gh", "pr", "create"]:
                return subprocess.CompletedProcess(
                    args, 1, "", "a pull request for branch already exists")
            if args[:3] == ["gh", "pr", "list"]:
                return subprocess.CompletedProcess(
                    args, 0, "https://github.com/up/stream/pull/9\n", "")
            return subprocess.CompletedProcess(args, 0, "", "")

        monkeypatch.setattr(pr, "_run", fake)
        monkeypatch.setattr(pr.subprocess, "run", fake)
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_X")
        out = pr.publish(repo, patch, {
            "push_repo": "me/fork", "upstream": "up/stream",
            "branch": "fix/3-c", "title": "fix: z"})
        assert out["ok"] is True
        assert out["pr"].endswith("/pull/9")


class TestDcoSignOff:
    """DCO bot 只认 commit message 里的 Signed-off-by。

    2026-10-30 实录：vllm#64811 / #64800、milvus#54926 全部挂着 DCO 红灯，
    因为这些 PR 的 commit 是我们拼的、没带 trailer。Playbook §4.3 要求查
    DCO，代码里从没实现过。
    """

    def _publish(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        patch = tmp_path / "change.patch"
        patch.write_text("--- a/x\n+++ b/x\n", encoding="utf-8")
        calls: list[list[str]] = []

        def fake(args, cwd=None, **kw):
            calls.append(list(args))
            if "--jq" in args and ".default_branch" in args:
                return subprocess.CompletedProcess(args, 0, "main", "")
            if args[:3] == ["gh", "pr", "create"]:
                return subprocess.CompletedProcess(args, 0, "https://x/pull/1\n", "")
            return subprocess.CompletedProcess(args, 0, "", "")

        monkeypatch.setattr(pr, "_run", fake)
        monkeypatch.setattr(pr.subprocess, "run", fake)
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_X")
        pr.publish(repo, patch, {"push_repo": "me/fork", "upstream": "up/stream",
                                 "branch": "fix/1-a", "title": "fix: thing"})
        return calls

    def test_commit_carries_signed_off_by(self, tmp_path, monkeypatch):
        calls = self._publish(tmp_path, monkeypatch)
        commit = next(c for c in calls if "commit" in c)
        msg = commit[commit.index("-qm") + 1]
        assert "Signed-off-by:" in msg
        # 首行仍是 PR 标题，trailer 在空行之后
        assert msg.splitlines()[0] == "fix: thing"
        assert msg.splitlines()[1] == ""
        assert msg.splitlines()[2].startswith("Signed-off-by:")


class TestWorkflowGate:
    """workflow 只应在证据链满足时才发布。"""

    def test_workflow_refuses_without_evidence(self):
        wf = (Path(__file__).resolve().parents[1]
              / ".github" / "workflows" / "oss-scout-repair.yml").read_text(encoding="utf-8")
        # 证据闸必须在真正调用发布之前出现
        assert "evidence chain not satisfied" in wf
        gate = wf.index("evidence chain not satisfied")
        call = wf.index("publish_from_env(job")
        assert gate < call, "evidence gate must precede the publish call"


class TestAutoFork:
    """fork 不存在时必须自动创建。

    2026-10-30 实测：langchain 和 pulumi 两次都通过了全部验证，最后一步
    push 报 'Repository not found' —— xixi-box/<repo> 还没建。每次靠人工补一步
    流程就不算跑通。
    """

    def test_existing_fork_returns_default_branch(self, monkeypatch):
        from scout_v2.workers import publish_remote as pr

        def fake(args, cwd=None):
            return subprocess.CompletedProcess(args, 0, "dev\n", "")

        monkeypatch.setattr(pr, "_run", fake)
        assert pr._ensure_fork("me/fork", "tok", "up/stream") == "dev"

    def test_missing_fork_is_created_then_polled(self, monkeypatch):
        from scout_v2.workers import publish_remote as pr
        calls: list[list[str]] = []
        state = {"exists": False}

        def fake(args, cwd=None):
            calls.append(list(args))
            if "-X" in args and "POST" in args:
                state["exists"] = True
                return subprocess.CompletedProcess(args, 0, "", "")
            if state["exists"]:
                return subprocess.CompletedProcess(args, 0, "main\n", "")
            return subprocess.CompletedProcess(args, 1, "", "Not Found")

        monkeypatch.setattr(pr, "_run", fake)
        monkeypatch.setattr(pr.time if hasattr(pr, "time") else __import__("time"),
                            "sleep", lambda *_: None, raising=False)
        got = pr._ensure_fork("me/fork", "tok", "up/stream")
        assert got == "main"
        assert any("POST" in c for c in calls), "should call the fork API"

    def test_fork_creation_failure_returns_none(self, monkeypatch):
        from scout_v2.workers import publish_remote as pr

        def fake(args, cwd=None):
            return subprocess.CompletedProcess(args, 1, "", "Resource not accessible")

        monkeypatch.setattr(pr, "_run", fake)
        import time as _t
        monkeypatch.setattr(_t, "sleep", lambda *_: None)
        assert pr._ensure_fork("me/fork", "tok", "up/stream") is None
