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


@pytest.fixture()
def fake(monkeypatch):
    f = TestSyncDiff._Fake()
    monkeypatch.setattr(fb.feishu_app, "tenant_token", lambda: "tok")
    monkeypatch.setattr(fb.feishu_app, "_request", f.request)
    monkeypatch.setattr(fb.feishu_app, "app_configured", lambda: True)
    monkeypatch.setenv("FEISHU_BITABLE_APP_TOKEN", "app1")
    return f


class TestSyncDiff:
    def test_explicit_complete_empty_snapshot_prunes_old_rows(self, tmp_path, fake):
        fake.records = {"r1": {"键": "a/b#1"}}
        out = fb.sync_table(tmp_path, [], table_name=fb.CAND_TABLE_NAME,
                            fields=fb.CAND_FIELDS, to_fields=fb.cand_row_to_fields,
                            binding_key="cand_table_id", allow_empty=True)
        assert out["pruned"] == 1
        assert fake.deleted == ["r1"]

    def test_unknown_empty_snapshot_keeps_old_rows(self, tmp_path, fake):
        fake.records = {"r1": {"键": "a/b#1"}}
        out = fb.sync_rows(tmp_path, [])
        assert out["pruned"] == 0
        assert fake.records

    def test_sync_preserves_other_table_bindings(self, tmp_path, fake):
        fb.save_binding(tmp_path, {"app_token": "app1", "repo_table_id": "repos"})
        fb.sync_rows(tmp_path, [_row()])
        assert fb.load_binding(tmp_path)["repo_table_id"] == "repos"

    def test_changing_base_does_not_reuse_old_table_ids(self, tmp_path, fake):
        fb.save_binding(tmp_path, {"app_token": "old", "pr_table_id": "old_table"})
        fb.sync_rows(tmp_path, [_row()])
        assert fb.load_binding(tmp_path)["pr_table_id"] == "tbl1"

    class _Fake:
        """按操作记账的假飞书 API。"""

        def __init__(self):
            self.records = {}
            self.created = []
            self.updated = []
            self.deleted = []

        def request(self, method, url, payload=None, token=None):
            if "/tables?" in url:
                return {"code": 0, "data": {"items": [
                    {"table_id": "tbl1", "name": fb.TABLE_NAME}]}}
            if url.endswith("/tables"):
                return {"code": 0, "data": {"table_id": "tbl1"}}
            if "/fields?page_size=" in url:
                # ensure_columns 的字段列表：PR 表已有所需字段
                names = {"键", "仓库", "PR号", "标题", "状态", "最近动作",
                         "回复状态", "修改状态", "创建时间", "最近活动",
                         "关闭时间", "链接", "备注"}
                items = [{"field_name": n} for n in names]
                return {"code": 0, "data": {"items": items}}
            if url.endswith("/fields") and method == "POST":
                return {"code": 0}
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
            if url.endswith("records/batch_delete"):
                for rid in payload["records"]:
                    self.deleted.append(rid)
                    self.records.pop(rid, None)
                return {"code": 0}
            raise AssertionError(f"unexpected url {url}")

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
        """table_id 按表名各存一份（base 里有多张表，互相不覆盖）。"""
        fb.sync_rows(tmp_path, [_row()])
        binding = json.loads((tmp_path / "state" / "feishu_bitable.json")
                             .read_text(encoding="utf-8"))
        assert binding["app_token"] == "app1"
        assert binding["pr_table_id"] == "tbl1"

    def test_closed_rows_are_kept_never_deleted(self, fake, tmp_path):
        fb.sync_rows(tmp_path, [_row(state="open")])
        fb.sync_rows(tmp_path, [_row(state="closed")])
        assert len(fake.records) == 1
        assert fake.records["r1"]["状态"] == "closed"


class TestLedgerScope:
    """台账口径：closed 未合并 / 自有仓库 / optica 不进表，进了的也要删掉。"""

    def test_unmerged_closed_excluded(self, monkeypatch):
        monkeypatch.setattr(mp, "all_my_prs", lambda limit=100: [
            _row(state="closed"), _row(state="merged"), _row(state="open")])
        rows = mp.build_pr_rows(Path("."))
        assert {r["state"] for r in rows} == {"merged", "open"}

    def test_own_repos_excluded(self, monkeypatch):
        monkeypatch.setattr(mp, "all_my_prs", lambda limit=100: [
            _row(repo="xixi-box/safedock", number=1),
            _row(repo="wangshun-china/interview-web", number=1),
            _row(repo="docker/compose", number=9)])
        rows = mp.build_pr_rows(Path("."))
        assert [r["repo"] for r in rows] == ["docker/compose"]

    def test_optica_excluded(self, monkeypatch):
        monkeypatch.setattr(mp, "all_my_prs", lambda limit=100: [
            _row(repo="Deep-Optica/SkillsMaster", number=3),
            _row(repo="wxyu-deep-optica/skill-vdr", number=4),
            _row(repo="qdrant/qdrant", number=5)])
        assert [r["repo"] for r in mp.build_pr_rows(Path("."))] == ["qdrant/qdrant"]

    def test_sync_prunes_out_of_scope_records(self, fake, tmp_path):
        # 直灌一条口径外的（模拟"口径收紧之前已经进了表"的历史记录）
        def keys_in_table():
            return {fs.get("键") for fs in fake.records.values()}

        fb.sync_rows(tmp_path, [_row(repo="xixi-box/safedock", number=1,
                                     state="merged")])
        assert "xixi-box/safedock#1" in keys_in_table()
        out = fb.sync_rows(tmp_path, [_row(repo="qdrant/qdrant", number=5,
                                           state="merged")])
        assert out["pruned"] == 1
        assert "xixi-box/safedock#1" not in keys_in_table()
        assert "qdrant/qdrant#5" in keys_in_table()

    def test_empty_rows_never_prunes(self, fake, tmp_path):
        """GitHub 全挂时 rows 为空——那不是"全删"的信号。"""
        fb.sync_rows(tmp_path, [_row()])
        out = fb.sync_rows(tmp_path, [])
        assert out.get("pruned", 0) == 0
        assert len(fake.records) == 1


class TestNumberNormalization:
    """数字列回传字符串不归一化，指纹永远对不上——扫描表 1300+ 行每 15
    分钟被整表重写一遍就是这么来的。"""

    def test_issue_number_and_stars_normalized(self, monkeypatch, tmp_path):
        captured = {}

        class Fake:
            def request(self, method, url, payload=None, token=None):
                if "/tables?" in url:
                    return {"code": 0, "data": {"items": [
                        {"table_id": "tblS", "name": fb.SCAN_TABLE_NAME}]}}
                if "/fields?page_size=" in url:
                    # 已存在的列与 SCAN_FIELDS 对齐（含新拆的粗筛/细筛原因），
                    # 否则 ensure_columns 会尝试 POST 建字段
                    return {"code": 0, "data": {"items": [
                        {"field_name": f["field_name"]} for f in fb.SCAN_FIELDS]}}
                if url.endswith("/fields"):
                    return {"code": 0, "data": {}}
                if "/records?page_size=" in url:
                    # 模拟真实回传形态：其他列原样回来，唯独数字列
                    # 变成字符串（飞书读端的已知行为）
                    return {"code": 0, "data": {"items": [
                        {"record_id": "r1", "fields": {
                            "键": "a/b#9", "仓库": "a/b", "issue号": "9",
                            "状态": "queued",
                            "链接": {"text": "a/b#9",
                                     "link": "https://github.com/a/b/issues/9"},
                            "发现时间": 1793840400000}}]}}
                raise AssertionError(url)

        fake = Fake()
        monkeypatch.setattr(fb.feishu_app, "tenant_token", lambda: "tok")
        monkeypatch.setattr(fb.feishu_app, "_request", fake.request)
        monkeypatch.setenv("FEISHU_BITABLE_APP_TOKEN", "app1")

        rows = [{"key": "a/b#9", "status": "queued", "why": "",
                 "first_seen": "2026-11-05T01:00:00"}]
        out = fb.sync_table(tmp_path, rows, table_name=fb.SCAN_TABLE_NAME,
                            fields=fb.SCAN_FIELDS,
                            to_fields=fb.scan_row_to_fields,
                            binding_key="scan_table_id")
        # 存量行的 issue号 是字符串 "9"，写入侧是 int 9——归一化后应当
        # 被识别为"无变化"而不是整行重写
        assert out["updated"] == 0, out
        assert out["skipped"] == 1


class TestCandidateRows:
    """候选池表：黄=排队中（含等确认），绿=已解决（PR 已开出），
    红=已失效（stale/竞争失败/超限/躺超 3 天没轮上）。"""

    def _entry(self, status, updated="2026-11-05T01:00:00"):
        return {"id": "a/b#9", "repo": "a/b", "number": 9, "title": "t",
                "lane": "open", "status": status, "updated_at": updated,
                "discovered_at": "2026-11-01T00:00:00",
                "ai_verdict": {"usable": True, "reason": ""}, "blockers": []}

    def test_candidate_table_only_keeps_queued(self, tmp_path, monkeypatch):
        """候选池 = 当前真实在排队的（用户裁定 2026-11-05）。

        已解决去 PR 记录表查；已失效/范围外/历史一律不进表——
        表里不留死行，处理状态永远不会为空。
        """
        (tmp_path / "queue").mkdir()
        (tmp_path / "queue" / "candidates.json").write_text(json.dumps({
            "a/b#1": self._entry("new"),
            "a/b#2": self._entry("pr_open"),                      # 已解决 → 踢
            "a/b#3": self._entry("aborted_stale"),                # 已失效 → 踢
            "a/b#4": self._entry("needs_maintainer_confirm"),     # 等确认 → 留
            "a/b#5": self._entry("queued_hist"),                  # 历史 → 踢
            "a/b#6": self._entry("out_of_scope"),                 # 范围外 → 踢
            "a/b#7": self._entry("new", updated="2026-10-01T00:00:00"),  # 躺超3天 → 踢
        }), encoding="utf-8")
        rows = mp.build_candidate_rows(tmp_path)
        states = {r["key"]: r["queue_state"] for r in rows}
        assert set(states) == {"a/b#1", "a/b#4"}
        assert all(v == "排队中" for v in states.values())

    def test_scan_rows_carry_queue_state(self, tmp_path, monkeypatch):
        (tmp_path / "state").mkdir()
        (tmp_path / "state" / "seen.json").write_text(
            json.dumps({"a/b#9": {"first_seen": "2026-11-05T01:00:00",
                                  "status": "queued"}}), encoding="utf-8")
        (tmp_path / "queue").mkdir()
        (tmp_path / "queue" / "candidates.json").write_text(json.dumps({
            "a/b#9": self._entry("pr_open")}), encoding="utf-8")
        rows = mp.build_scan_rows(tmp_path)
        assert rows[0]["queue_state"] == "已解决"

    def test_cand_converter_colors_and_fields(self):
        f = fb.cand_row_to_fields({
            "key": "a/b#9", "title": "t", "lane": "open",
            "queue_state": "排队中", "ai_verdict": "ok",
            "discovered_at": "2026-11-05T01:00:00Z",
            "updated_at": "2026-11-05T02:00:00Z",
            "pr_url": "https://github.com/a/b/pull/1", "note": "n"})
        assert f["处理状态"] == "排队中"
        assert f["issue号"] == 9
        assert f["PR链接"]["link"].endswith("/pull/1")
