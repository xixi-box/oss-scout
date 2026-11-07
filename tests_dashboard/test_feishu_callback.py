"""飞书卡片回调的单元测试（challenge / token 校验 / form 提交）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

TOKEN = "verif-token-123"


def _make_configs(tmp_path: Path) -> None:
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "queue").mkdir(parents=True, exist_ok=True)
    (tmp_path / "configs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "configs" / "cloudflare.yaml").write_text(
        yaml.safe_dump({"account_id": "", "api_token": "", "kv_namespace_id": "",
                        "ingest_token": "t"}), encoding="utf-8")
    (tmp_path / "configs" / "repositories.yaml").write_text(
        yaml.safe_dump({"defaults": {"requires_invite": False},
                        "repositories": {}}), encoding="utf-8")
    (tmp_path / "configs" / "pr_gates.yaml").write_text(
        yaml.safe_dump({"defaults": {"requires_invite": True},
                        "cla": {}, "repositories": {}}), encoding="utf-8")
    (tmp_path / "configs" / "dashboard.yaml").write_text(
        "auto_repair_seconds: 0\n", encoding="utf-8")
    (tmp_path / "configs" / "v2.yaml").write_text(
        "budget: {}\nfeatures: {}\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("# playbook\n", encoding="utf-8")
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    (tmp_path / "queue" / "candidates.json").write_text("{}", encoding="utf-8")


@pytest.fixture()
def client(tmp_path: Path, monkeypatch) -> TestClient:
    """默认处于"已配置 token"状态——绝大多数测试关心的是业务分支。"""
    monkeypatch.setenv("FEISHU_VERIFICATION_TOKEN", TOKEN)
    _make_configs(tmp_path)
    from dashboard.app import create_app
    return TestClient(create_app(tmp_path, auth_enabled=False, password="pw"))


@pytest.fixture()
def client_unconfigured(tmp_path: Path, monkeypatch) -> TestClient:
    """未配置 verification token 的实例：fail-closed，卡片动作全拒。"""
    monkeypatch.delenv("FEISHU_VERIFICATION_TOKEN", raising=False)
    _make_configs(tmp_path)
    from dashboard.app import create_app
    return TestClient(create_app(tmp_path, auth_enabled=False, password="pw"))


def cb(client: TestClient, payload):
    return client.post("/feishu/callback", json=payload)


def card_click(action_value, form_value, token=TOKEN):
    """真实的 card.action.trigger 报文（schema 2.0）。

    verification token 在 header.token；event.token 是"更新卡片凭证"，
    两者不是同一个东西——曾经混用导致每次真实点击都 403。
    """
    return {"schema": "2.0",
            "header": {"event_id": "e1", "token": token,
                       "event_type": "card.action.trigger"},
            "event": {"operator": {"open_id": "ou_x"},
                      "token": "card-update-credential",
                      "action": {"value": action_value,
                                 "tag": "button",
                                 "form_value": form_value}}}


class TestChallenge:
    def test_challenge_echoed(self, client):
        r = cb(client, {"type": "url_verification",
                        "url_verification": {"challenge": "abc123", "token": TOKEN}})
        assert r.status_code == 200
        assert r.json()["challenge"] == "abc123"


class TestAuth:
    def test_wrong_token_rejected(self, client, monkeypatch):
        monkeypatch.setenv("FEISHU_VERIFICATION_TOKEN", TOKEN)
        r = cb(client, card_click({}, {}, token="wrong"))
        assert r.status_code == 403

    def test_unconfigured_rejects(self, client_unconfigured):
        """fail-closed：token 未配置时拒绝所有卡片动作（这个端点能发 GitHub 评论）。"""
        r = cb(client_unconfigured, card_click({}, {}))
        assert r.status_code == 503


class TestFormSubmit:
    def test_send_posts_comment_and_marks_sent(self, client, tmp_path, monkeypatch):
        rp = tmp_path / "state" / "pr_replies.json"
        rp.write_text(json.dumps({"docker/compose#7": {
            "status": "draft", "draft": "old text", "repo": "docker/compose",
            "number": 7}}), encoding="utf-8")
        calls = []

        def fake_run(cmd, **k):
            calls.append(cmd)
            import subprocess
            return subprocess.CompletedProcess(cmd, 0, "", "")

        import subprocess as sp_mod
        monkeypatch.setattr(sp_mod, "run", fake_run)
        r = cb(client, card_click(
            {"do": "send", "repo": "docker/compose", "number": "7"},
            {"reply_text": "edited by human in feishu"}))
        assert r.status_code == 200
        assert calls and any("/issues/7/comments" in str(a) for a in calls[0])
        data = json.loads(rp.read_text(encoding="utf-8"))
        assert data["docker/compose#7"]["status"] == "sent"
        assert data["docker/compose#7"]["sent_body"] == "edited by human in feishu"

    def test_missing_fields_rejected(self, client):
        r = cb(client, card_click({"repo": "docker/compose"}, {}))
        assert r.status_code == 400


class TestDraftsOnPrsPage:
    """「起草之后我怎么知道」——草稿必须出现在 PR 管理页顶部，可编辑可发送。"""

    def test_pending_draft_shown_on_prs_page(self, client, tmp_path):
        rp = tmp_path / "state" / "pr_replies.json"
        rp.write_text(json.dumps({"docker/compose#7": {
            "status": "draft",
            "to_author": "maintainer",
            "drafted_at": "2026-11-04T01:00:00",
            "draft": "Thanks! Fixed in the new commit."}}), encoding="utf-8")
        r = client.get("/prs")
        assert r.status_code == 200
        assert "回复草稿" in r.text
        assert "Thanks! Fixed in the new commit." in r.text
        assert "docker/compose#7" in r.text

    def test_sent_draft_not_shown_as_pending(self, client, tmp_path):
        rp = tmp_path / "state" / "pr_replies.json"
        rp.write_text(json.dumps({"docker/compose#7": {
            "status": "sent", "draft": "already sent"}}), encoding="utf-8")
        r = client.get("/prs")
        assert r.status_code == 200
        assert "already sent" not in r.text


class TestReplySendApi:
    def test_send_requires_all_fields(self, client):
        r = client.post("/api/pr/reply/send", data={"repo": "docker/compose"})
        assert r.status_code == 400

    def test_send_posts_and_marks_sent(self, client, tmp_path, monkeypatch):
        rp = tmp_path / "state" / "pr_replies.json"
        rp.write_text(json.dumps({"docker/compose#7": {
            "status": "draft", "draft": "Thanks, fixed."}}), encoding="utf-8")
        posted = []

        def fake_run(cmd, **k):
            posted.append(cmd)
            import subprocess
            return subprocess.CompletedProcess(cmd, 0,
                                               "https://x/comment", "")

        import subprocess as sp_mod
        monkeypatch.setattr(sp_mod, "run", fake_run)
        r = client.post("/api/pr/reply/send", data={
            "repo": "docker/compose", "number": 7, "body": "Thanks, fixed."})
        assert r.status_code == 200
        assert posted and any("/issues/7/comments" in str(a) for a in posted[0])
        data = json.loads(rp.read_text(encoding="utf-8"))
        assert data["docker/compose#7"]["status"] == "sent"

    def test_send_failure_reported(self, client, monkeypatch):
        import subprocess as sp
        monkeypatch.setattr(sp, "run", lambda cmd, **k: sp.CompletedProcess(
            cmd, 1, "", "gh: failed"))
        r = client.post("/api/pr/reply/send", data={
            "repo": "docker/compose", "number": 7, "body": "x"})
        assert r.status_code == 502

    def test_dismiss_marks_dismissed(self, client, tmp_path):
        rp = tmp_path / "state" / "pr_replies.json"
        rp.write_text(json.dumps({"docker/compose#7": {"status": "draft"}}),
                      encoding="utf-8")
        r = client.post("/api/pr/reply/dismiss",
                        data={"repo": "docker/compose", "number": 7})
        assert r.status_code == 200
        data = json.loads(rp.read_text(encoding="utf-8"))
        assert data["docker/compose#7"]["status"] == "dismissed"


class TestSendOnlyPostsComment:
    """按钮只发回复：代码修改由 pr-fix 自动推送，不需要人放行。

    这条边界是刻意的——人审的是"对外文字"，代码改动只要验过就该推。
    所以点击时不做任何 git 操作，只发一条评论。
    """

    def _with_fix(self, tmp_path, state):
        rp = tmp_path / "state" / "pr_replies.json"
        rp.write_text(json.dumps({"docker/compose#7": {
            "status": "draft", "draft": "x", "fix_state": state,
            "fix_sha": "c" * 40, "fix_fork": "xixi-box/real"}}),
            encoding="utf-8")

    def _submit(self, client):
        return cb(client, card_click(
            {"do": "send", "repo": "docker/compose", "number": "7"},
            {"reply_text": "thanks, done"}))

    def test_no_git_operations_on_send(self, client, tmp_path, monkeypatch):
        self._with_fix(tmp_path, "pushed")
        import subprocess as sp
        seen = []

        def fake_run(cmd, **k):
            seen.append(" ".join(cmd))
            return sp.CompletedProcess(cmd, 0, "https://x", "")

        monkeypatch.setattr(sp, "run", fake_run)
        r = self._submit(client)
        assert r.status_code == 200
        assert any("/issues/7/comments" in c for c in seen)
        assert not any("refs/heads" in c for c in seen),             "send must not touch git refs"

    def test_send_works_even_when_fix_failed(self, client, tmp_path, monkeypatch):
        """改代码失败了也不该堵住回复——人可以说"我看过了，先回这句"。"""
        self._with_fix(tmp_path, "failed")
        import subprocess as sp
        monkeypatch.setattr(sp, "run", lambda cmd, **k: sp.CompletedProcess(
            cmd, 0, "https://x", ""))
        r = self._submit(client)
        assert r.status_code == 200
        data = json.loads((tmp_path / "state" / "pr_replies.json")
                          .read_text(encoding="utf-8"))
        assert data["docker/compose#7"]["status"] == "sent"


class TestFixResultEndpoint:
    """workflow 回报"改动已暂存"，卡片据此从 pending 翻成 ready。"""

    def test_requires_token(self, client):
        r = client.post("/api/pr/reply/fix-result", json={"repo": "docker/compose", "number": 1})
        # 测试环境 cloudflare.yaml 里 ingest_token 是 "t"
        assert r.status_code == 403

    def test_records_pushed_state(self, client, tmp_path):
        rp = tmp_path / "state" / "pr_replies.json"
        rp.write_text(json.dumps({"docker/compose#1": {"status": "draft"}}), encoding="utf-8")
        r = client.post("/api/pr/reply/fix-result",
                        headers={"X-Ingest-Token": "t"},
                        json={"repo": "docker/compose", "number": 1, "test_ok": True,
                              "test_result": "go test rc=0",
                              "changed_files": ["pkg/watch/x_test.go"],
                              "pushed": True,
                              "pushed_sha": "d" * 40,
                              "fork_repo": "xixi-box/b"})
        assert r.status_code == 200
        data = json.loads(rp.read_text(encoding="utf-8"))
        e = data["docker/compose#1"]
        assert e["fix_state"] == "pushed"
        assert e["fix_sha"] == "d" * 40

    def test_failed_fix_is_not_reported_as_pushed(self, client, tmp_path):
        rp = tmp_path / "state" / "pr_replies.json"
        rp.write_text(json.dumps({"docker/compose#1": {"status": "draft"}}), encoding="utf-8")
        client.post("/api/pr/reply/fix-result", headers={"X-Ingest-Token": "t"},
                    json={"repo": "docker/compose", "number": 1, "pushed": False,
                          "test_ok": False, "test_result": "rc=1"})
        e = json.loads(rp.read_text(encoding="utf-8"))["docker/compose#1"]
        assert e["fix_state"] == "failed"


class TestTokenLocation:
    """verification token 只在 header.token。

    2026-11-04 实录：飞书已经打进来了，日志里三次 403。原因是回调只查
    body.token / event.token，而 2.0 卡片回调把验证 token 放在 header.token，
    event.token 是"更新卡片凭证"（30 分钟、限 2 次）——两者完全不同。
    """

    def test_header_token_is_accepted(self, client, tmp_path):
        rp = tmp_path / "state" / "pr_replies.json"
        rp.write_text(json.dumps({"docker/compose#7": {"status": "draft"}}),
                      encoding="utf-8")
        import subprocess as sp
        import unittest.mock as m
        with m.patch.object(sp, "run", lambda cmd, **k: sp.CompletedProcess(
                cmd, 0, "https://x", "")):
            r = cb(client, card_click(
                {"do": "send", "repo": "docker/compose", "number": "7"},
                {"reply_text": "ok"}))
        assert r.status_code == 200

    def test_event_token_alone_is_rejected(self, client):
        """event.token 是更新卡片的凭证，不能拿来当验证依据。"""
        payload = card_click({"do": "send", "repo": "docker/compose", "number": "7"},
                             {"reply_text": "x"})
        payload["header"]["token"] = ""
        r = cb(client, payload)
        assert r.status_code == 403

    def test_response_wraps_card_as_raw(self, client, tmp_path):
        """卡片更新要求 card={type:"raw", data:{...}}；裸 card 不会生效。"""
        rp = tmp_path / "state" / "pr_replies.json"
        rp.write_text(json.dumps({"docker/compose#7": {"status": "draft"}}),
                      encoding="utf-8")
        import subprocess as sp
        import unittest.mock as m
        with m.patch.object(sp, "run", lambda cmd, **k: sp.CompletedProcess(
                cmd, 0, "https://x", "")):
            r = cb(client, card_click(
                {"do": "send", "repo": "docker/compose", "number": "7"},
                {"reply_text": "ok"}))
        body = r.json()
        assert body["card"]["type"] == "raw"
        assert "elements" in body["card"]["data"]


class TestInlineReplyRouting:
    """行内发言行内回：回调按卡片按钮带的 kind/comment_id 选端点。"""

    def _submit_with(self, client, value):
        return cb(client, card_click(value, {"reply_text": "done"}))

    def test_inline_kind_uses_replies_endpoint(self, client, tmp_path):
        rp = tmp_path / "state" / "pr_replies.json"
        rp.write_text(json.dumps({"docker/compose#7": {"status": "draft"}}),
                      encoding="utf-8")
        import subprocess as sp
        seen = []

        def fake_run(cmd, **k):
            seen.append(" ".join(cmd))
            return sp.CompletedProcess(cmd, 0, "https://x", "")

        import unittest.mock as m
        with m.patch.object(sp, "run", fake_run):
            r = self._submit_with(client, {"do": "send", "repo": "docker/compose",
                                           "number": "7", "kind": "inline",
                                           "comment_id": "12345"})
        assert r.status_code == 200
        assert any("pulls/7/comments/12345/replies" in c for c in seen), seen
        assert not any("/issues/7/comments" in c for c in seen)
        data = json.loads(rp.read_text(encoding="utf-8"))
        assert data["docker/compose#7"]["sent_kind"] == "inline"
        assert data["docker/compose#7"]["sent_comment_id"] == "12345"

    def test_issue_kind_stays_issue_level(self, client, tmp_path):
        rp = tmp_path / "state" / "pr_replies.json"
        rp.write_text(json.dumps({"docker/compose#7": {"status": "draft"}}),
                      encoding="utf-8")
        import subprocess as sp
        seen = []

        def fake_run(cmd, **k):
            seen.append(" ".join(cmd))
            return sp.CompletedProcess(cmd, 0, "https://x", "")

        import unittest.mock as m
        with m.patch.object(sp, "run", fake_run):
            r = self._submit_with(client, {"do": "send", "repo": "docker/compose",
                                           "number": "7", "kind": "issue",
                                           "comment_id": ""})
        assert r.status_code == 200
        assert any("/issues/7/comments" in c for c in seen)
        assert not any("replies" in c for c in seen)

    def test_missing_kind_defaults_to_issue(self, client, tmp_path):
        rp = tmp_path / "state" / "pr_replies.json"
        rp.write_text(json.dumps({"docker/compose#7": {"status": "draft"}}),
                      encoding="utf-8")
        import subprocess as sp
        seen = []

        def fake_run(cmd, **k):
            seen.append(" ".join(cmd))
            return sp.CompletedProcess(cmd, 0, "https://x", "")

        import unittest.mock as m
        with m.patch.object(sp, "run", fake_run):
            r = self._submit_with(client, {"do": "send", "repo": "docker/compose",
                                           "number": "7"})
        assert r.status_code == 200
        assert any("/issues/7/comments" in c for c in seen)

    def test_card_button_carries_source(self):
        from scout_v2 import feishu_app
        card = feishu_app.build_reply_card(
            "a/b", 1, "t", "m", "c", "rc", "d",
            source_kind="inline", comment_id=12345)
        btn = [n for n in card["elements"]
               if n.get("tag") == "form"][0]["elements"][1]
        assert btn["value"]["kind"] == "inline"
        assert btn["value"]["comment_id"] == "12345"


class TestNoChangeOutcome:
    """opencode 判"无需变更"必须落账，否则监控循环会反复重派、反复发卡。

    2026-11-05 实录：5 条已修复的 PR 被循环重派 3 小时，每轮给用户发
    同一张卡片。根因：no-change 的 workflow 没回调，看板状态停在
    "进行中"，派发去重表又没有记录。
    """

    def test_no_change_records_nochange_state(self, client, tmp_path):
        rp = tmp_path / "state" / "pr_replies.json"
        rp.write_text(json.dumps({"docker/compose#1": {"status": "draft"}}), encoding="utf-8")
        r = client.post("/api/pr/reply/fix-result",
                        headers={"X-Ingest-Token": "t"},
                        json={"repo": "docker/compose", "number": 1, "no_change": True,
                              "pushed": False})
        assert r.status_code == 200
        e = json.loads(rp.read_text(encoding="utf-8"))["docker/compose#1"]
        assert e["fix_state"] == "nochange"

    def test_nochange_card_status_line(self):
        from scout_v2.feishu_app import build_reply_card
        card = build_reply_card("a/b", 1, "t", "m", "c", "rc", "d",
                                fix_state="nochange")
        blob = json.dumps(card, ensure_ascii=False)
        assert "无需变更" in blob
