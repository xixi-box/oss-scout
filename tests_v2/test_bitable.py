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

    def test_pr_discovery_time_uses_first_seen_of_linked_issue(self, tmp_path, monkeypatch):
        from scout_v2.state_store import ClaimLedger

        ledger = ClaimLedger(tmp_path / "state/scout_v2.sqlite3")
        row = _row()
        assert ledger.reserve_publication("Asia/Shanghai", 5, "job", row["repo"],
                                          "xixi-box:fix/issue-11-abcdef0-ab123456", "xixi-box/oss-scout")
        ledger.finish_publication("job", day="2026-11-02", url=row["url"])
        ledger.close()
        (tmp_path / "state/seen.json").write_text(json.dumps({
            "Docker/Compose#11": {"first_seen": "2026-11-01T05:50:07Z"}
        }))
        monkeypatch.setattr(mp, "all_my_prs", lambda: [row])
        record, = mp.build_pr_rows(tmp_path)
        assert record["discovered_at"] == "2026-11-01T05:50:07Z"
        assert fb.row_to_fields(record)["发现时间"] == fb.row_to_fields(record)["创建时间"] - 86400000

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
    def test_pr_columns_and_link(self):
        assert fb.row_to_fields(_row()) == {"键": [{
            "type": "url", "text": "docker/compose#14450",
            "link": "https://github.com/docker/compose/pull/14450",
        }], "标题": "fix: thing", "PR 状态": "merged", "发现时间": None, "创建时间": 1793598607000}
        assert fb.repo_row_to_fields({"repo": "a/b"})["键"][0]["link"] == "https://github.com/a/b"
        assert fb.cand_row_to_fields({"key": "a/b#9"})["键"][0]["link"] == "https://github.com/a/b/issues/9"


    def test_single_select_migration_and_unchanged_options(self, monkeypatch):
        spec = next(f for f in fb.PR_FIELDS if f["field_name"] == "PR 状态")
        field = {"field_id": "fld1", "field_name": spec["field_name"], "type": 1}
        updates = []

        def request(method, url, payload=None, token=None):
            if method == "GET":
                return {"code": 0, "data": {"items": [field]}}
            assert method == "PUT" and url.endswith("/fld1")
            updates.append(payload)
            field.update(payload)
            return {"code": 0}

        monkeypatch.setattr(fb.feishu_app, "tenant_token", lambda: "tok")
        monkeypatch.setattr(fb.feishu_app, "_request", request)
        fb.ensure_columns("base", "table", [spec])
        fb.ensure_columns("base", "table", [spec])
        assert updates == [spec]
        options = spec["property"]["options"]
        assert len({o["color"] for o in options}) == 3

    def test_coarse_rejection_does_not_claim_fine_screen_ran(self):
        fields = fb.scan_row_to_fields({"key": "a/b#9", "status": "blocked",
                                        "coarse_reason": "label", "fine_reason": "not screened"})
        assert fields["最终结果"] == "粗筛淘汰 · label"
        assert fields["粗筛结果"] == "label"
        assert fields["细筛结果"] == "未执行"
        assert set(fields) == {field["field_name"] for field in fb.SCAN_FIELDS}


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

    def test_scan_result_hyperlink_survives_readback_without_rewrite(self, fake, tmp_path):
        row = {"key": "a/b#1", "status": "queued", "pr_url": "https://github.com/a/b/pull/2"}
        kwargs = dict(table_name=fb.SCAN_TABLE_NAME, fields=fb.SCAN_FIELDS,
                      to_fields=fb.scan_row_to_fields, binding_key="scan_table_id")
        assert fb.sync_table(tmp_path, [row], **kwargs)["created"] == 1
        link = fake.records["r1"]["最终结果"][0]
        assert link == {"type": "url", "text": "已开 PR · a/b#2", "link": row["pr_url"]}
        fake.records["r1"]["最终结果"][0] = {**link, "extra": "returned by Feishu"}
        assert fb.sync_table(tmp_path, [row], **kwargs)["skipped"] == 1

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
                    self.created.append(fb._key_label(rec["fields"]["键"]))
                return {"code": 0}
            if url.endswith("records/batch_update"):
                for rec in payload["records"]:
                    self.records[rec["record_id"]].update(rec["fields"])
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
        fake.records = {"r1": {"键": "docker/compose#14450", "状态": "open"}}
        out = fb.sync_rows(tmp_path, [_row(state="merged")])
        assert out["updated"] == 1 and out["created"] == 0
        assert fake.records["r1"]["键"] == fb.row_to_fields(_row())["键"]
        assert fb.sync_rows(tmp_path, [_row()])["skipped"] == 1

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
        assert fake.records["r1"] == fb.row_to_fields(_row(state="closed"))


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
            return {fb._key_label(fs.get("键")) for fs in fake.records.values()}

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


class TestCandidateRows:
    def _entry(self, status, updated="2026-11-05T01:00:00"):
        return {"id": "a/b#9", "repo": "a/b", "number": 9, "title": "t",
                "lane": "open", "status": status, "updated_at": updated,
                "discovered_at": "2026-11-01T00:00:00",
                "ai_verdict": {"usable": True, "reason": ""}, "blockers": []}

    def test_candidate_table_tracks_real_actions_start(self, tmp_path):
        (tmp_path / "queue").mkdir()
        (tmp_path / "queue" / "candidates.json").write_text(json.dumps({
            "a/b#1": self._entry("new"),
            "a/b#2": self._entry("pr_open"),
            "a/b#3": self._entry("aborted_stale"),
            "a/b#4": self._entry("needs_maintainer_confirm"),
            "a/b#5": self._entry("queued"),
            "a/b#6": dict(self._entry("staged"), discovered_at=None,
                          staged_at="2026-11-05T04:00:00+08:00"),
            "a/b#7": self._entry("running"),
            "A/B#8": self._entry("running"),
        }), encoding="utf-8")
        rows = mp.build_candidate_rows(tmp_path, running_keys={"a/b#8"})
        fields = {r["key"]: fb.cand_row_to_fields(r) for r in rows}
        assert {k: f["处理状态"] for k, f in fields.items()} == {
            "a/b#1": "待执行", "a/b#6": "暂存",
            "a/b#7": "待执行", "A/B#8": "执行中",
        }
        assert fields["a/b#6"]["入队时间"] == fb._timestamp("2026-11-05T04:00:00+08:00")

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



def test_scan_preserves_screen_results_and_shows_later_abort(tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "queue").mkdir()
    keys = ["a/b#1", "a/b#2"]
    (tmp_path / "state/seen.json").write_text(json.dumps({
        key: {"status": "queued", "fine_reason": "OpenJev 0.99"} for key in keys
    }), encoding="utf-8")
    (tmp_path / "queue/candidates.json").write_text(json.dumps({
        keys[0]: {"status": "aborted_competed", "abort_reason": "已有修复 PR · a/b#3"},
        keys[1]: {"status": "aborted_competed"},
    }), encoding="utf-8")
    (tmp_path / "state/worklog.jsonl").write_text(json.dumps({
        "action": "pipeline_abort", "target": keys[1], "result": "competed: hits=['a/b#4']",
    }) + "\n", encoding="utf-8")

    rows = [fb.scan_row_to_fields(row) for row in mp.build_scan_rows(tmp_path)]
    assert rows[0]["最终结果"] == "粗筛淘汰 · 已有修复 PR · a/b#3"
    assert rows[1]["最终结果"].startswith("粗筛淘汰")
    assert "a/b#4" in rows[1]["最终结果"]
    assert rows[0]["粗筛结果"] == "已有修复 PR · a/b#3"
    assert all(row["细筛结果"] == "OpenJev 0.99" for row in rows)



def test_scan_outcomes_follow_candidate_and_publication_state(tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "queue").mkdir()
    entries = {
        "a/b#1": {"status": "new"},
        "a/b#2": {"status": "staged"},
        "a/b#3": {"status": "running"},
        "a/b#4": {"status": "running"},
        "a/b#5": {"status": "aborted_no_evidence", "abort_reason": "缺少复现证据"},
        "a/b#6": {"status": "needs_cla", "abort_reason": "人工签署"},
        "a/b#7": {"status": "pr_open", "pr_url": "https://github.com/a/b/pull/17"},
        "a/b#8": {"status": "escalated", "abort_reason": "控制器重启"},
    }
    (tmp_path / "state/seen.json").write_text(json.dumps({
        key: {"status": "queued", "fine_reason": "OpenJev 0.99"}
        for key in [*entries, "a/b#9"]
    }), encoding="utf-8")
    (tmp_path / "queue/candidates.json").write_text(json.dumps(entries), encoding="utf-8")
    rows = mp.build_scan_rows(tmp_path, {"a/b#3"}, {"a/b#8": "https://github.com/a/b/pull/18"})
    fields = {row["key"]: fb.scan_row_to_fields(row) for row in rows}
    assert [fields[f"a/b#{i}"]["最终结果"] for i in (1, 2, 3, 4, 5, 6, 9)] == [
        "待执行", "暂存", "执行中", "待执行", "修复失败 · 缺少复现证据",
        "粗筛淘汰 · 人工签署", None,
    ]
    assert fields["a/b#7"]["最终结果"][0]["link"] == "https://github.com/a/b/pull/17"
    assert fields["a/b#8"]["最终结果"][0]["link"] == "https://github.com/a/b/pull/18"
    assert fields["a/b#6"]["粗筛结果"] == "人工签署"
    assert all(row["细筛结果"] == "OpenJev 0.99" for row in fields.values())
