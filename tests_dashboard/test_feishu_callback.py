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


class TestChallenge:
    def test_challenge_echoed(self, client):
        r = cb(client, {"type": "url_verification",
                        "url_verification": {"challenge": "abc123", "token": TOKEN}})
        assert r.status_code == 200
        assert r.json()["challenge"] == "abc123"


class TestAuth:
    def test_wrong_token_rejected(self, client, monkeypatch):
        monkeypatch.setenv("FEISHU_VERIFICATION_TOKEN", TOKEN)
        r = cb(client, {"event": {"token": "wrong", "action": {"value": {},
                                                               "form_value": {}}}})
        assert r.status_code == 403

    def test_unconfigured_rejects(self, client_unconfigured):
        """fail-closed：token 未配置时拒绝所有卡片动作（这个端点能发 GitHub 评论）。"""
        r = cb(client_unconfigured, {"event": {"action": {"value": {},
                                                         "form_value": {}}}})
        assert r.status_code == 503


class TestFormSubmit:
    def test_send_posts_comment_and_marks_sent(self, client, tmp_path, monkeypatch):
        rp = tmp_path / "state" / "pr_replies.json"
        rp.write_text(json.dumps({"acme/real#7": {
            "status": "draft", "draft": "old text", "repo": "acme/real",
            "number": 7}}), encoding="utf-8")
        calls = []

        def fake_run(cmd, **k):
            calls.append(cmd)
            import subprocess
            return subprocess.CompletedProcess(cmd, 0, "", "")

        import subprocess as sp_mod
        monkeypatch.setattr(sp_mod, "run", fake_run)
        r = cb(client, {"event": {
            "token": TOKEN,
            "action": {
                "value": {"do": "send", "repo": "acme/real", "number": "7"},
                "form_value": {"reply_text": "edited by human in feishu"}}}})
        assert r.status_code == 200
        assert calls and any("/issues/7/comments" in str(a) for a in calls[0])
        data = json.loads(rp.read_text(encoding="utf-8"))
        assert data["acme/real#7"]["status"] == "sent"
        assert data["acme/real#7"]["sent_body"] == "edited by human in feishu"

    def test_missing_fields_rejected(self, client):
        r = cb(client, {"event": {"token": TOKEN,
                                  "action": {"value": {"repo": "acme/real"},
                                             "form_value": {}}}})
        assert r.status_code == 400


class TestDraftsOnPrsPage:
    """「起草之后我怎么知道」——草稿必须出现在 PR 管理页顶部，可编辑可发送。"""

    def test_pending_draft_shown_on_prs_page(self, client, tmp_path):
        rp = tmp_path / "state" / "pr_replies.json"
        rp.write_text(json.dumps({"acme/real#7": {
            "status": "draft",
            "to_author": "maintainer",
            "drafted_at": "2026-11-04T01:00:00",
            "draft": "Thanks! Fixed in the new commit."}}), encoding="utf-8")
        r = client.get("/prs")
        assert r.status_code == 200
        assert "回复草稿" in r.text
        assert "Thanks! Fixed in the new commit." in r.text
        assert "acme/real#7" in r.text

    def test_sent_draft_not_shown_as_pending(self, client, tmp_path):
        rp = tmp_path / "state" / "pr_replies.json"
        rp.write_text(json.dumps({"acme/real#7": {
            "status": "sent", "draft": "already sent"}}), encoding="utf-8")
        r = client.get("/prs")
        assert r.status_code == 200
        assert "already sent" not in r.text


class TestReplySendApi:
    def test_send_requires_all_fields(self, client):
        r = client.post("/api/pr/reply/send", data={"repo": "acme/real"})
        assert r.status_code == 400

    def test_send_posts_and_marks_sent(self, client, tmp_path, monkeypatch):
        rp = tmp_path / "state" / "pr_replies.json"
        rp.write_text(json.dumps({"acme/real#7": {
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
            "repo": "acme/real", "number": 7, "body": "Thanks, fixed."})
        assert r.status_code == 200
        assert posted and any("/issues/7/comments" in str(a) for a in posted[0])
        data = json.loads(rp.read_text(encoding="utf-8"))
        assert data["acme/real#7"]["status"] == "sent"

    def test_send_failure_reported(self, client, monkeypatch):
        import subprocess as sp
        monkeypatch.setattr(sp, "run", lambda cmd, **k: sp.CompletedProcess(
            cmd, 1, "", "gh: failed"))
        r = client.post("/api/pr/reply/send", data={
            "repo": "acme/real", "number": 7, "body": "x"})
        assert r.status_code == 502

    def test_dismiss_marks_dismissed(self, client, tmp_path):
        rp = tmp_path / "state" / "pr_replies.json"
        rp.write_text(json.dumps({"acme/real#7": {"status": "draft"}}),
                      encoding="utf-8")
        r = client.post("/api/pr/reply/dismiss",
                        data={"repo": "acme/real", "number": 7})
        assert r.status_code == 200
        data = json.loads(rp.read_text(encoding="utf-8"))
        assert data["acme/real#7"]["status"] == "dismissed"
