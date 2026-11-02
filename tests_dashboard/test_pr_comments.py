"""PR 评论即时通道的接收端测试。

背景：GitHub 的 webhook 注册在**仓库**上，要接收上游仓库（mypy、milvus 等）的
issue_comment 需要那个仓库的 admin 权限，我们没有。所以走 Cloudflare Worker
轮询 comments?since= → POST /api/ingest/pr-comments 这条路。

这里只验证接收端：鉴权、去重、分类、落盘。**不发任何对外文字**——维护者提了
意见该怎么回是需要人判断的事。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

TOKEN = "test-ingest-token"


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    (tmp_path / "state").mkdir(parents=True)
    (tmp_path / "queue").mkdir(parents=True)
    (tmp_path / "configs").mkdir(parents=True)
    (tmp_path / "configs" / "cloudflare.yaml").write_text(
        yaml.safe_dump({"account_id": "", "api_token": "", "kv_namespace_id": "",
                        "ingest_token": TOKEN}), encoding="utf-8")
    (tmp_path / "configs" / "repositories.yaml").write_text(
        yaml.safe_dump({"defaults": {"requires_invite": False},
                        "repositories": {"acme/real": {"lane": "open"}}}),
        encoding="utf-8")
    (tmp_path / "configs" / "pr_gates.yaml").write_text(
        yaml.safe_dump({"defaults": {"requires_invite": True}, "repositories": {}}),
        encoding="utf-8")
    (tmp_path / "configs" / "dashboard.yaml").write_text(
        "auto_repair_seconds: 0\n", encoding="utf-8")
    (tmp_path / "configs" / "v2.yaml").write_text("budget: {}\nfeatures: {}\n",
                                                  encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("# playbook\n", encoding="utf-8")
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    (tmp_path / "queue" / "candidates.json").write_text("{}", encoding="utf-8")

    from dashboard.app import create_app
    app = create_app(tmp_path, auth_enabled=False, password="pw")
    return TestClient(app)


def _post(client: TestClient, body, token: str = TOKEN):
    return client.post("/api/ingest/pr-comments", json=body,
                       headers={"X-Ingest-Token": token})


class TestAuth:
    def test_wrong_token_is_forbidden(self, client):
        r = _post(client, {"events": []}, token="nope")
        assert r.status_code == 403

    def test_missing_token_is_forbidden(self, client):
        r = client.post("/api/ingest/pr-comments", json={"events": []})
        assert r.status_code == 403

    def test_bad_body_is_rejected(self, client):
        assert _post(client, {"events": "not-a-list"}).status_code == 400


class TestClassification:
    def test_review_comment_is_recorded(self, client, tmp_path):
        r = _post(client, {"events": [{
            "repo": "acme/real", "number": 7, "comment_id": 1001,
            "author": "maintainer",
            "body": "please add a test for the empty case"}]})
        assert r.status_code == 200
        assert r.json()["new"] == 1
        rows = [json.loads(l) for l in
                (tmp_path / "state" / "pr_watch.jsonl").read_text(encoding="utf-8").splitlines()]
        assert rows[-1]["action"] == "address_review"
        assert rows[-1]["repo"] == "acme/real"
        assert rows[-1]["number"] == 7

    def test_duplicate_comment_is_dropped(self, client):
        body = {"events": [{"repo": "acme/real", "number": 7, "comment_id": 2002,
                            "author": "m", "body": "please fix"}]}
        assert _post(client, body).json()["new"] == 1
        # Worker 的窗口有 10 分钟重叠，同一条会被推多次
        assert _post(client, body).json()["new"] == 0

    def test_ci_style_comment_stays_wait(self, client, tmp_path):
        _post(client, {"events": [{
            "repo": "acme/real", "number": 8, "comment_id": 3003,
            "author": "changeset-bot[bot]",
            "body": "No changeset found in this PR"}]})
        rows = [json.loads(l) for l in
                (tmp_path / "state" / "pr_watch.jsonl").read_text(encoding="utf-8").splitlines()]
        assert rows[-1]["action"] == "wait"

    def test_malformed_event_is_skipped(self, client):
        r = _post(client, {"events": [
            {"repo": "", "number": 0, "comment_id": ""},   # 缺字段
            "not-a-dict",
            {"repo": "acme/real", "number": 9, "comment_id": 4004,
             "author": "m", "body": "please change this"},
        ]})
        assert r.status_code == 200
        assert r.json()["new"] == 1


class TestReplyDraftApi:
    """L3：草拟自动、发送人审。send/dismiss 都要审计。"""

    def test_send_requires_all_fields(self, client):
        r = client.post("/api/pr/reply/send", data={"repo": "acme/real"})
        assert r.status_code == 400

    def test_send_posts_and_marks_sent(self, client, tmp_path, monkeypatch):
        import subprocess as sp
        # 预置一条草稿
        rp = tmp_path / "state" / "pr_replies.json"
        rp.write_text(json.dumps({"acme/real#7": {
            "status": "draft", "draft": "Thanks, fixed in the new commit."}}),
            encoding="utf-8")
        posted = []

        def fake_run(cmd, **k):
            if cmd[:2] == ["gh", "pr"]:
                posted.append(cmd)
            return sp.CompletedProcess(cmd, 0,
                                       "https://github.com/acme/real/pull/7#comment", "")

        monkeypatch.setattr("subprocess.run", fake_run)
        r = client.post("/api/pr/reply/send", data={
            "repo": "acme/real", "number": 7,
            "body": "Thanks, fixed in the new commit."})
        assert r.status_code == 200
        assert posted and posted[0][3] == "7"
        data = json.loads(rp.read_text(encoding="utf-8"))
        assert data["acme/real#7"]["status"] == "sent"

    def test_send_failure_reported(self, client, tmp_path, monkeypatch):
        import subprocess as sp
        monkeypatch.setattr("subprocess.run", lambda cmd, **k: sp.CompletedProcess(
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

    def test_replies_listing(self, client, tmp_path):
        rp = tmp_path / "state" / "pr_replies.json"
        rp.write_text(json.dumps({"acme/real#7": {"status": "draft"}}),
                      encoding="utf-8")
        r = client.get("/api/pr/replies")
        assert r.status_code == 200
        assert r.json()["replies"]["acme/real#7"]["status"] == "draft"
