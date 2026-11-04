"""多维表格同步：行拼装、字段转换、upsert 差分。

这张表是"我们所有 PR 的台账"——用户在飞书里看质量用的，所以：
不删记录（关闭的 PR 留着）、键是 repo#number（重跑不重复）、
GitHub 拉挂了行也要产出（本地增强列可以为空）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import importlib.util  # noqa: E402

from scout_v2 import feishu_bitable as fb  # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    "monitor_prs", Path(__file__).resolve().parents[1] / "scripts" / "monitor_prs.py")
mp = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mp)


def _row(**kw):
    base = {"repo": "docker/compose", "number": 14450, "title": "fix: thing",
            "state": "merged", "url": "https://github.com/docker/compose/pull/14450",
            "created_at": "2026-11-02T05:50:07Z",
            "updated_at": "2026-11-04T09:00:00Z",
            "closed_at": "2026-11-03T12:00:00Z"}
    base.update(kw)
    return base


class TestBuildRows:
    def _setup_state(self, root):
        (root / "state").mkdir(parents=True, exist_ok=True)
        (root / "state" / "pr_watch.jsonl").write_text(
            json.dumps({"repo": "docker/compose", "number": "14450",
                        "action": "address_review", "reason": "review from ndeloof"}) + "\n",
            encoding="utf-8")
        (root / "state" / "pr_replies.json").write_text(json.dumps({
            "docker/compose#14450": {"status": "sent",
                                     "sent_body": "done",
                                     "fix_state": "pushed",
                                     "fix_test_result": "go test rc=0"}}),
            encoding="utf-8")

    def test_merges_local_state(self, tmp_path, monkeypatch):
        self._setup_state(tmp_path)
        monkeypatch.setattr(mp, "all_my_prs", lambda limit=100: [_row()])
        rows = mp.build_pr_rows(tmp_path)
        assert len(rows) == 1
        r = rows[0]
        assert r["action"] == "address_review"
        assert r["reply_status"] == "已发送"
        assert r["fix_state"] == "已推送"
        assert "go test rc=0" in r["note"]

    def test_rows_survive_when_github_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mp, "all_my_prs", lambda limit=100: [])
        assert mp.build_pr_rows(tmp_path) == []

    def test_state_files_missing_is_fine(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mp, "all_my_prs", lambda limit=100: [_row()])
        rows = mp.build_pr_rows(tmp_path)
        assert rows[0]["action"] == "unknown"
        assert rows[0]["reply_status"] == ""

    def test_all_my_prs_dedupes_open_and_closed(self, monkeypatch):
        raw = {
            "open": [{"repository": {"nameWithOwner": "a/b"}, "number": 1,
                      "title": "t", "state": "OPEN", "url": "u",
                      "createdAt": "c", "updatedAt": "u", "closedAt": None}],
            "closed": [{"repository": {"nameWithOwner": "a/b"}, "number": 2,
                        "title": "t2", "state": "MERGED", "url": "u2",
                        "createdAt": "c", "updatedAt": "u", "closedAt": "x"}],
        }
        def fake_gh(args, timeout=90):
            state = args[args.index("--state") + 1]
            return json.dumps(raw[state])

        monkeypatch.setattr(mp, "_gh", fake_gh)
        got = mp.all_my_prs()
        assert {(r["repo"], r["number"]) for r in got} == {("a/b", 1), ("a/b", 2)}
        states = {r["number"]: r["state"] for r in got}
        assert states[2] == "merged"


class TestRowToFields:
    def test_key_and_link(self):
        f = fb.row_to_fields(_row())
        assert f["键"] == "docker/compose#14450"
        assert f["链接"]["link"].endswith("/pull/14450")
        assert f["PR号"] == 14450

    def test_datetimes_are_ms(self):
        f = fb.row_to_fields(_row())
        assert isinstance(f["创建时间"], int)
        assert f["创建时间"] > 10**12      # 毫秒级时间戳

    def test_empty_optional_fields_are_omitted(self):
        f = fb.row_to_fields(_row(action="", reply_status="", fix_state="", note=""))
        for col in ("最近动作", "回复状态", "修改状态", "备注", "回复状态"):
            assert col not in f


class TestSyncDiff:
    class _Fake:
        """按操作记账的假飞书 API。"""

        def __init__(self):
            self.records = {}
            self.created = []
            self.updated = []

        def request(self, method, url, payload=None, token=None):
            if "/tables?" in url:
                return {"code": 0, "data": {"items": [
                    {"table_id": "tbl1", "name": fb.TABLE_NAME}]}}
            if url.endswith("/tables"):
                return {"code": 0, "data": {"table_id": "tbl1"}}
            if "/records?page_size=" in url:
                items = [{"record_id": rid, "fields": fs}
                         for rid, fs in self.records.items()]
                return {"code": 0, "data": {"items": items, "has_more": False}}
            if url.endswith("records/batch_create"):
                for rec in payload["records"]:
                    rid = f"r{len(self.records)+1}"
                    self.records[rid] = rec["fields"]
                    self.created.append(rec["fields"]["键"])
                return {"code": 0}
            if url.endswith("records/batch_update"):
                for rec in payload["records"]:
                    self.records[rec["record_id"]] = rec["fields"]
                    self.updated.append(rec["record_id"])
                return {"code": 0}
            raise AssertionError(f"unexpected url {url}")

    @pytest.fixture()
    def fake(self, monkeypatch, tmp_path):
        f = self._Fake()
        monkeypatch.setattr(fb.feishu_app, "tenant_token", lambda: "tok")
        monkeypatch.setattr(fb.feishu_app, "_request", f.request)
        monkeypatch.setattr(fb.feishu_app, "app_configured", lambda: True)
        monkeypatch.setattr(fb, "list_bases",
                            lambda: [{"app_token": "app1", "name": "台账"}])
        return f

    def test_first_sync_creates(self, fake, tmp_path):
        out = fb.sync_rows(tmp_path, [_row(), _row(repo="a/b", number=2)])
        assert out["created"] == 2 and out["updated"] == 0
        assert fake.created == ["docker/compose#14450", "a/b#2"]

    def test_second_sync_skips_unchanged(self, fake, tmp_path):
        rows = [_row()]
        fb.sync_rows(tmp_path, rows)
        out = fb.sync_rows(tmp_path, rows)
        assert out["skipped"] == 1 and out["created"] == 0

    def test_changed_row_updates_not_creates(self, fake, tmp_path):
        fb.sync_rows(tmp_path, [_row(state="open")])
        out = fb.sync_rows(tmp_path, [_row(state="merged")])
        assert out["updated"] == 1 and out["created"] == 0

    def test_binding_is_cached(self, fake, tmp_path):
        fb.sync_rows(tmp_path, [_row()])
        binding = json.loads((tmp_path / "state" / "feishu_bitable.json")
                             .read_text(encoding="utf-8"))
        assert binding == {"app_token": "app1", "table_id": "tbl1"}

    def test_closed_rows_are_kept_never_deleted(self, fake, tmp_path):
        fb.sync_rows(tmp_path, [_row(state="open")])
        fb.sync_rows(tmp_path, [_row(state="closed")])
        assert len(fake.records) == 1
        assert fake.records["r1"]["状态"] == "closed"
