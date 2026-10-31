"""CLA 处理的单元测试。

三种机制里只有 comment_sign 能自动签；另外两种必须人工在浏览器签署。
判错的代价是双向的：把「要人工签」当成「能自动」＝PR 开出去挂着没人管；
把「能自动」当成「要人工」＝白白拦住一个本来能做的候选。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scout_v2.workers import cla  # noqa: E402


def _root(tmp_path, cla_cfg):
    import yaml
    (tmp_path / "configs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "configs" / "pr_gates.yaml").write_text(
        yaml.safe_dump({"defaults": {"requires_invite": True},
                        "cla": cla_cfg, "repositories": {}}, allow_unicode=True),
        encoding="utf-8")
    return tmp_path


class TestRequirement:
    def test_no_config_means_no_cla(self, tmp_path):
        root = _root(tmp_path, {})
        assert cla.requirement(root, "acme/real") is None

    def test_comment_sign_repo(self, tmp_path):
        root = _root(tmp_path, {"acme/x": {"service": "comment_sign",
                                           "phrase": "I hereby sign"}})
        assert cla.can_auto_sign(root, "acme/x") is True

    def test_cla_assistant_cannot_auto_sign(self, tmp_path):
        """cla-assistant 要 OAuth，agent 做不到也不该做。"""
        root = _root(tmp_path, {"acme/y": {"service": "cla-assistant"}})
        assert cla.can_auto_sign(root, "acme/y") is False


class TestPreflight:
    def test_manual_repo_blocks_before_pr(self, tmp_path):
        """只能人工签的仓库，开 PR 前就该拦住——挂着的 PR 更难堪。"""
        root = _root(tmp_path, {"acme/y": {"service": "cla-assistant"}})
        got = cla.preflight(root, "acme/y")
        assert got["blocking"] is True
        assert got["need_manual"] is True
        assert "cla-assistant" in got["detail"]

    def test_auto_repo_does_not_block(self, tmp_path):
        root = _root(tmp_path, {"acme/x": {"service": "comment_sign",
                                           "phrase": "I hereby sign"}})
        got = cla.preflight(root, "acme/x")
        assert got["blocking"] is False

    def test_repo_without_cla_does_not_block(self, tmp_path):
        root = _root(tmp_path, {})
        assert cla.preflight(root, "acme/z")["blocking"] is False


class TestSign:
    def test_already_signed_is_noop(self, tmp_path, monkeypatch):
        root = _root(tmp_path, {"acme/x": {"service": "comment_sign",
                                           "phrase": "I hereby sign"}})
        calls = []

        def fake(args, timeout=90):
            calls.append(list(args))
            return subprocess.CompletedProcess(
                args, 0,
                '{"statusCheckRollup":[{"name":"CLA","conclusion":"SUCCESS"}],'
                '"comments":[]}', "")

        monkeypatch.setattr(cla, "_gh", fake)
        got = cla.sign(root, "acme/x", 12)
        assert got["action"] == "already_signed"
        assert not any("comment" in c for c in calls)

    def test_not_signed_posts_phrase(self, tmp_path, monkeypatch):
        root = _root(tmp_path, {"acme/x": {"service": "comment_sign",
                                           "phrase": "I have read the CLA"}})
        posted = []

        def fake(args, timeout=90):
            if args[:2] == ["pr", "comment"]:
                posted.append(args)
            return subprocess.CompletedProcess(
                args, 0,
                '{"statusCheckRollup":[{"name":"CLA","conclusion":"FAILURE"}],'
                '"comments":[]}', "")

        monkeypatch.setattr(cla, "_gh", fake)
        got = cla.sign(root, "acme/x", 12)
        assert got["action"] == "signed"
        assert posted and posted[0][posted[0].index("--body") + 1] == "I have read the CLA"

    def test_manual_service_is_refused(self, tmp_path, monkeypatch):
        """对着 cla-assistant 的仓库发口令没有意义，必须明确拒绝而不是静默失败。"""
        root = _root(tmp_path, {"acme/y": {"service": "cla-assistant"}})
        monkeypatch.setattr(cla, "_gh", lambda *a, **k: subprocess.CompletedProcess(
            [], 0, "{}", ""))
        got = cla.sign(root, "acme/y", 7)
        assert got["ok"] is False
        assert got["action"] == "need_manual"

    def test_repo_without_cla_is_noop(self, tmp_path):
        root = _root(tmp_path, {})
        assert cla.sign(root, "acme/z", 3)["action"] == "not_required"

    def test_unknown_state_does_not_assume_signed(self, tmp_path, monkeypatch):
        """看不到 CLA 状态时按未签处理：宁可多发一次口令，也不要漏签。"""
        root = _root(tmp_path, {"acme/x": {"service": "comment_sign",
                                           "phrase": "sign me"}})
        posted = []

        def fake(args, timeout=90):
            if args[:2] == ["pr", "comment"]:
                posted.append(args)
            return subprocess.CompletedProcess(args, 0, '{"comments":[]}', "")

        monkeypatch.setattr(cla, "_gh", fake)
        cla.sign(root, "acme/x", 5)
        assert posted


class TestSignUrl:
    """人工签的仓库要给出准确的入口链接——让人自己去翻文档是最容易被忽略的一步。"""

    def test_cla_assistant_url(self, tmp_path):
        root = _root(tmp_path, {"acme/y": {"service": "cla-assistant"}})
        assert cla.sign_url(root, "acme/y") == "https://cla-assistant.io/acme/y"

    def test_microsoft_url(self, tmp_path):
        root = _root(tmp_path, {"acme/m": {"service": "microsoft"}})
        assert "cla.opensource.microsoft.com" in cla.sign_url(root, "acme/m")

    def test_no_cla_has_no_url(self, tmp_path):
        root = _root(tmp_path, {})
        assert cla.sign_url(root, "acme/z") == ""

    def test_blocking_preflight_carries_url(self, tmp_path):
        root = _root(tmp_path, {"acme/y": {"service": "cla-assistant"}})
        got = cla.preflight(root, "acme/y")
        assert got["blocking"] is True
        assert got["url"].endswith("acme/y")
