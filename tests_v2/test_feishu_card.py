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
        # kind/comment_id 随按钮带给回调，决定回复落在哪一层
        assert btns[0]["value"]["do"] == "send"
        assert btns[0]["value"]["repo"] == "acme/widget"
        assert btns[0]["value"]["number"] == "42"
        assert btns[0]["value"]["kind"] == "issue"

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


class TestCommentIsNotTruncated:
    """评审内容不能被静默截断。

    2026-11-04 实录（docker/compose#14450）：ndeloof 的 740 字评审被 [:400]
    切在 `["!/rep` 处，卡片上只剩前半段客套，"One blocking point: ..." 和
    "Please also add a regression test ..." 两条要求全都不见了。人看到的是
    半句话，AI 起草时也少看了 240 字。
    """

    REVIEW = (
        "Thanks, removing the `*` special case is the right fix: I checked that "
        "with `*` then `!src`, `data/log`, `.env` and `node_modules/...` are now "
        "ignored while `src/**` is not, and deny-lists are unchanged.\n"
        "\n"
        "One blocking point: `TestNewDockerPatternMatcher/Wildcard_with_exclusion` "
        "asserts the behavior this PR removes, and it now fails. It needs to be "
        'updated to expect `["/repo/*", "!/repo/file.txt"]`.\n'
        "\n"
        "Please also add a regression test for the allow-list at the `Matches` "
        "level (e.g. `*` then `!src`: `data/log` ignored, `src/index.js` not), "
        "since the existing test only looks at the parsed patterns.")

    def _text(self, comment):
        card = feishu_app.build_reply_card(
            "docker/compose", 14450, "title", "ndeloof", comment, "rc", "draft")
        return "\n".join(e["text"]["content"] for e in card["elements"]
                         if e.get("tag") == "div")

    def test_realistic_review_survives_intact(self):
        text = self._text(self.REVIEW)
        assert len(self.REVIEW) > 400, "fixture must exceed the old 400-char cut"
        # 两条要求都必须出现
        assert "blocking point" in text.lower()
        assert "Matches" in text
        assert "src/index.js" in text

    def test_nothing_is_dropped_for_a_long_but_normal_review(self):
        assert self.REVIEW in self._text(self.REVIEW)

    def test_newlines_are_preserved(self):
        """分条评审压成一行后，读的人分不清哪句是一条要求。"""
        text = self._text(self.REVIEW)
        assert "\n" in text
        assert "Thank" in text and "blocking point" in text

    def test_pathological_comment_is_marked_as_cut(self):
        """真超长时明示被截断，不让人以为对方只说了半句。"""
        huge = "x" * 5000
        text = self._text(huge)
        assert "此处显示前" in text
        assert "5000" in text       # 报出原文长度


class TestCardLinkAndState:
    """卡片要能点仓库直达 PR；按钮文案要说明它到底做什么。"""

    def _card(self, **kw):
        args = dict(repo="docker/compose", number=14450, issue_title="t",
                    source_author="ndeloof", source_comment="c",
                    root_cause="rc", draft="reply")
        args.update(kw)
        return feishu_app.build_reply_card(**args)

    def _all_text(self, card):
        return json.dumps(card, ensure_ascii=False)

    def test_repo_is_a_hyperlink_to_the_pr(self):
        card = self._card(pr_url="https://github.com/docker/compose/pull/14450")
        blob = self._all_text(card)
        assert "[docker/compose#14450](https://github.com/docker/compose/pull/14450)" in blob

    def test_link_falls_back_to_canonical_url(self):
        card = self._card()
        blob = self._all_text(card)
        assert "https://github.com/docker/compose/pull/14450" in blob

    def test_fix_pushed_is_reported(self):
        blob = self._all_text(self._card(fix_state="pushed"))
        assert "已修改并推送" in blob

    def test_fix_pending_says_in_progress(self):
        blob = self._all_text(self._card(fix_state="pending"))
        assert "代码修改进行中" in blob

    def test_fix_failed_is_reported(self):
        blob = self._all_text(self._card(fix_state="failed"))
        assert "未完成" in blob

    def test_button_always_only_sends(self):
        """代码修改自动推送；人对文字负责，所以按钮只发回复。"""
        for st in ("none", "pending", "pushed", "failed"):
            blob = self._all_text(self._card(fix_state=st))
            assert "一键发送回复" in blob, st
            assert "推送修改" not in blob, st

    def test_status_line_absent_when_no_fix(self):
        blob = self._all_text(self._card(fix_state="none"))
        assert "已修改" not in blob



class TestInputIsMultiline:
    """草稿动辄三四百字，单行输入框只能看到开头一行，改不动。"""

    def _input(self):
        card = feishu_app.build_reply_card(
            "a/b", 1, "t", "m", "c", "rc", "x" * 300)
        return [n for n in _walk(card) if n.get("tag") == "input"][0]

    def test_input_is_multiline(self):
        assert self._input()["input_type"] == "multiline_text"

    def test_has_visible_rows(self):
        inp = self._input()
        assert inp.get("rows", 0) >= 4

    def test_grows_with_content(self):
        assert self._input().get("auto_resize") is True
        assert self._input().get("max_rows", 0) > 6

    def test_max_length_still_within_feishu_limit(self):
        assert self._input()["max_length"] <= 1000


class TestSentCard:
    def test_sent_card_replaces_the_form(self):
        card = feishu_app.build_sent_card("a/b", 7, "thanks!")
        assert card["header"]["template"] == "green"
        tags = [e.get("tag") for e in card["elements"]]
        assert "form" not in tags, "sent card must not keep the input form"
        assert "thanks!" in json.dumps(card, ensure_ascii=False)

    def test_sent_card_links_to_pr(self):
        blob = json.dumps(feishu_app.build_sent_card(
            "docker/compose", 14450, "x",
            pr_url="https://github.com/docker/compose/pull/14450"),
            ensure_ascii=False)
        assert "[/pull/14450" in blob or "docker/compose#14450](" in blob


class TestCardUpdate:
    """发送后要主动 PATCH 原消息，否则回调响应丢了卡片就停在输入框。"""

    def test_patch_uses_message_id(self, monkeypatch):
        monkeypatch.setattr(feishu_app, "tenant_token", lambda: "tok")
        seen = {}

        def fake_request(method, url, payload=None, token=None):
            seen.update(method=method, url=url, payload=payload)
            return {"code": 0, "msg": "ok"}

        monkeypatch.setattr(feishu_app, "_request", fake_request)
        ok, err = feishu_app.update_card_message("om_123", {"elements": []})
        assert ok is True and err == ""
        assert seen["method"] == "PATCH"
        assert "om_123" in seen["url"]
        # content 必须是序列化后的字符串
        assert isinstance(seen["payload"]["content"], str)

    def test_missing_message_id_is_reported(self):
        ok, err = feishu_app.update_card_message("", {"elements": []})
        assert ok is False
        assert "message_id" in err
