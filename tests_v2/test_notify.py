"""飞书通知模块测试：webhook 解析、冷却、失败吞掉。

原则：通知永远不能影响主流程——任何异常都被吞掉，返回 False。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scout_v2 import notify  # noqa: E402


class TestWebhookResolution:
    def test_env_var_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://open.feishu.cn/hook/env")
        wf = tmp_path / "state" / "feishu_webhook.txt"
        wf.parent.mkdir(parents=True)
        wf.write_text("https://open.feishu.cn/hook/file", encoding="utf-8")
        assert notify._webhook_url() == "https://open.feishu.cn/hook/env"

    def test_file_fallback(self, monkeypatch, tmp_path):
        monkeypatch.delenv("FEISHU_WEBHOOK_URL", raising=False)
        notify.Path.__mul__ = None  # noop, keep linter quiet
        wf = tmp_path / "hook.txt"
        wf.write_text("https://open.feishu.cn/hook/file", encoding="utf-8")
        # monkeypatch the candidate path by patching os.environ dashboard root
        monkeypatch.setenv("OSSCOUT_DASHBOARD_ROOT", str(tmp_path))
        (tmp_path / "state").mkdir(exist_ok=True)
        (tmp_path / "state" / "feishu_webhook.txt").write_text(
            "https://open.feishu.cn/hook/file", encoding="utf-8")
        assert notify._webhook_url() == "https://open.feishu.cn/hook/file"

    def test_unconfigured_returns_empty(self, monkeypatch, tmp_path):
        monkeypatch.delenv("FEISHU_WEBHOOK_URL", raising=False)
        monkeypatch.setenv("OSSCOUT_DASHBOARD_ROOT", str(tmp_path / "nonexistent"))
        assert notify._webhook_url() == ""


class TestSend:
    def test_unconfigured_is_false_but_never_raises(self, monkeypatch):
        monkeypatch.delenv("FEISHU_WEBHOOK_URL", raising=False)
        monkeypatch.setattr(notify, "_webhook_url", lambda: "")
        assert notify.feishu_send("hi") is False

    def test_posts_text_payload(self, monkeypatch):
        monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://open.feishu.cn/hook/x")
        captured = {}

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def read(self):
                return b'{"code": 0}'

        def fake_urlopen(req, timeout=10):
            captured["payload"] = req.data
            captured["url"] = req.full_url
            return FakeResp()

        import urllib.request
        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(notify, "_LAST_SENT", 0.0)
        ok = notify.feishu_send("标题", "正文", force=True)
        assert ok is True
        assert "标题".encode("utf-8") in captured["payload"]
        assert "正文".encode("utf-8") in captured["payload"]
        assert "hook/" in captured["url"]

    def test_network_error_swallowed(self, monkeypatch):
        import urllib.request

        def boom(req, timeout=10):
            raise OSError("network down")

        monkeypatch.setattr(urllib.request, "urlopen", boom)
        monkeypatch.setattr(notify, "_LAST_SENT", 0.0)
        assert notify.feishu_send("hi", force=True) is False

    def test_cooldown_dedupes_bursts(self, monkeypatch):
        monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://open.feishu.cn/hook/x")
        calls = []

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def read(self):
                return b'{"code": 0}'

        def fake_urlopen(req, timeout=10):
            calls.append(req)
            return FakeResp()

        import urllib.request
        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        notify._LAST_SENT = 0.0
        assert notify.feishu_send("a", force=True) is True
        # 冷却期内（未 force）不发
        assert notify.feishu_send("b") is False
        assert len(calls) == 1
