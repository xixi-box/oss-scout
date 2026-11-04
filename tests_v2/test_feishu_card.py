"""飞书自建应用卡片：结构、群查找、草稿通知分支的单元测试。

这里守的是一条边界：**卡片内容必须够人做决定**（哪个仓库哪个 PR、issue 是什么、
我们为什么这么修、对方原话），否则"一键发送"按钮只是个陷阱。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scout_v2 import feishu_app  # noqa: E402


def _walk(node):
    """展开卡片里所有 dict（飞书卡片是嵌套结构，断言要能钻进去）。"""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


class TestReplyCard:
    def card(self, **kw):
        args = dict(repo="acme/widget", number=42,
                    issue_title="streaming drops args",
                    source_author="enyst",
                    source_comment="Could you add a regression test?",
                    root_cause="deltas bypass _merge_arguments",
                    draft="Thanks, added the test.")
        args.update(kw)
        return feishu_app.build_reply_card(**args)

    def test_card_has_header_and_elements(self):
        card = self.card()
        assert card["header"]["title"]["content"]
        assert isinstance(card["elements"], list) and card["elements"]

    def test_header_names_repo_and_number(self):
        title = self.card()["header"]["title"]["content"]
        assert "acme/widget#42" in title

    def test_issue_title_present(self):
        text = json.dumps(self.card(), ensure_ascii=False)
        assert "streaming drops args" in text

    def test_root_cause_present(self):
        text = json.dumps(self.card(), ensure_ascii=False)
        assert "_merge_arguments" in text

    def test_source_comment_and_author_present(self):
        text = json.dumps(self.card(), ensure_ascii=False)
        assert "enyst" in text
        assert "regression test" in text

    def test_draft_is_editable_default_value(self):
        inputs = [n for n in _walk(self.card()) if n.get("tag") == "input"]
        assert len(inputs) == 1
        assert inputs[0]["name"] == "reply_text"
        assert inputs[0]["default_value"] == "Thanks, added the test."

    def test_submit_button_carries_target(self):
        btns = [n for n in _walk(self.card())
                if n.get("tag") == "button" and n.get("action_type") == "form_submit"]
        assert len(btns) == 1
        assert btns[0]["value"] == {"do": "send", "repo": "acme/widget",
                                    "number": "42"}

    def test_long_input_is_truncated_not_rejected(self):
        """超长草稿不能把整张卡片弄成非法 JSON（飞书 input 上限 1000）。"""
        card = self.card(draft="x" * 5000, source_comment="y" * 5000)
        inputs = [n for n in _walk(card) if n.get("tag") == "input"]
        assert len(inputs[0]["default_value"]) <= 1000
        assert inputs[0]["max_length"] <= 1000


class TestChatLookup:
    def test_fixed_chat_id_skips_api(self, monkeypatch):
        """配了 FEISHU_CHAT_ID 就不该调 chats 接口（少一个权限依赖）。"""
        monkeypatch.setenv("FEISHU_CHAT_ID", "oc_fixed")

        def boom(*a, **k):
            raise AssertionError("should not call the API")

        monkeypatch.setattr(feishu_app, "_request", boom)
        assert feishu_app.find_group_chat_id() == "oc_fixed"

    def test_missing_scope_surfaces_error(self, monkeypatch):
        """缺 im:chat:readonly 时要能说出原因，不能只返回"没找到群"。"""
        monkeypatch.delenv("FEISHU_CHAT_ID", raising=False)
        monkeypatch.setattr(feishu_app, "tenant_token", lambda: "tok")

        import urllib.error

        def deny(*a, **k):
            raise urllib.error.HTTPError(
                "u", 400, "Bad Request", {},
                __import__("io").BytesIO(
                    json.dumps({"code": 99991672,
                                "msg": "Access denied. im:chat:readonly"}).encode()))

        monkeypatch.setattr(feishu_app.urllib.request, "urlopen", deny)
        items, err = feishu_app.list_chats()
        assert items == []
        assert "im:chat:readonly" in err


class TestSendCard:
    def test_no_token_is_reported(self, monkeypatch):
        monkeypatch.delenv("FEISHU_APP_ID", raising=False)
        monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
        monkeypatch.setattr(feishu_app, "tenant_token", lambda: "")
        ok, err = feishu_app.send_card_to_chat("oc_x", {"a": 1})
        assert ok is False
        assert "tenant token" in err

    def test_success(self, monkeypatch):
        monkeypatch.setattr(feishu_app, "tenant_token", lambda: "tok")
        monkeypatch.setattr(feishu_app, "_request",
                            lambda *a, **k: {"code": 0, "msg": "success"})
        ok, err = feishu_app.send_card_to_chat("oc_x", {"a": 1})
        assert ok is True
        assert err == ""

    def test_http_error_body_is_surfaced(self, monkeypatch):
        """飞书把"卡片非法"的真相放在错误体里；只报 HTTP 400 等于让人再猜一轮。"""
        import io
        import urllib.error

        monkeypatch.setattr(feishu_app, "tenant_token", lambda: "tok")

        def bad(*a, **k):
            raise urllib.error.HTTPError(
                "u", 400, "Bad Request", {}, io.BytesIO(json.dumps(
                    {"code": 230099,
                     "msg": "Failed to create card content, "
                            "ErrMsg: max_length exceed the default maximum 1000"}
                ).encode()))

        monkeypatch.setattr(feishu_app.urllib.request, "urlopen", bad)
        ok, err = feishu_app.send_card_to_chat("oc_x", {"a": 1})
        assert ok is False
        assert "max_length" in err


class TestDraftNotificationFallback:
    """应用没配好时必须回退 webhook 卡片，而不是静默丢通知。"""

    def _app(self, tmp_path):
        (tmp_path / "state").mkdir(parents=True, exist_ok=True)
        (tmp_path / "state" / "worklog.jsonl").write_text("", encoding="utf-8")
        from dashboard.app import create_app
        return create_app(tmp_path, auth_enabled=False, password="pw")

    def test_webhook_card_used_when_app_missing(self, tmp_path, monkeypatch):
        import dashboard.app as appmod

        create_app = self._app(tmp_path)
        assert create_app is not None
        sent = []

        monkeypatch.setattr(feishu_app, "app_configured", lambda: False)
        from scout_v2 import notify

        monkeypatch.setattr(notify, "feishu_send_card",
                            lambda *a, **k: sent.append((a, k)) or True)

        r = {"repo": "acme/widget", "number": 7, "latest_author": "bob",
             "latest_excerpt": "please add a test", "latest_body": "please add a test",
             "pr_title": "fix: thing"}
        draft = {"entry": {"draft": "Thanks, added.", "root_cause": "rc"}}
        appmod._notify_draft(tmp_path, r, draft)

        assert sent, "expected a fallback webhook notification"
        assert "acme/widget#7" in sent[0][0][0]

    def test_app_card_used_when_configured(self, tmp_path, monkeypatch):
        import dashboard.app as appmod

        self._app(tmp_path)
        monkeypatch.setattr(feishu_app, "app_configured", lambda: True)
        monkeypatch.setattr(feishu_app, "find_group_chat_id", lambda: "oc_g")
        cards = []

        def fake_send(chat, card):
            cards.append((chat, card))
            return True, ""

        monkeypatch.setattr(feishu_app, "send_card_to_chat", fake_send)

        r = {"repo": "acme/widget", "number": 7, "latest_author": "bob",
             "latest_excerpt": "please add a test", "latest_body": "please add a test",
             "pr_title": "fix: thing"}
        draft = {"entry": {"draft": "Thanks, added.", "root_cause": "rc"}}
        appmod._notify_draft(tmp_path, r, draft)

        assert cards and cards[0][0] == "oc_g"
        text = json.dumps(cards[0][1], ensure_ascii=False)
        assert "acme/widget#7" in text
        assert "Thanks, added." in text
        assert "rc" in text  # 根因要让人一眼判断修得对不对

    def test_notify_failure_never_raises(self, tmp_path, monkeypatch):
        """通知是旁路：任何异常都不能把 PR 监控循环打挂。"""
        import dashboard.app as appmod

        self._app(tmp_path)
        monkeypatch.setattr(feishu_app, "app_configured", lambda: True)
        monkeypatch.setattr(feishu_app, "find_group_chat_id", lambda: "oc_g")
        monkeypatch.setattr(feishu_app, "send_card_to_chat",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        appmod._notify_draft(tmp_path,
                             {"repo": "a/b", "number": 1, "latest_author": "x",
                              "latest_excerpt": "y", "pr_title": "t"},
                             {"entry": {"draft": "d"}})
