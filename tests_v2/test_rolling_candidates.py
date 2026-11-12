import datetime as dt
import json
from types import SimpleNamespace

import pytest

from dashboard import data
from scout_v2 import state_store
from scout_v2.feishu_bitable import scan_row_to_fields
from scout_v2.workers import auto_pipeline as pipeline
from scripts.monitor_prs import build_scan_rows, build_candidate_rows
from test_unified_queue import queue_root, read_queue, write_queue


def candidate(number, stamp, status="staged"):
    return {"id": f"a/b#{number}", "repo": "a/b", "number": number, "status": status,
            "discovered_at": stamp, "staged_at": stamp, "eligible_at": stamp,
            "ai_verdict": {"usable": True, "reason": "verified"}}


def seed(root, entries):
    write_queue(root, entries)
    (root / "state").mkdir(exist_ok=True)
    (root / "state/seen.json").write_text(json.dumps({key: {
        "status": "queued", "first_seen": entry["discovered_at"], "coarse_reason": "",
        "fine_reason": "verified"} for key, entry in entries.items()}), encoding="utf-8")


def test_a_b_c_d_rollovers_keep_todays_fifo_and_link_scan_and_candidate_tables(queue_root, monkeypatch):
    entries = {f"a/b#{i}": candidate(i, f"2026-11-11T10:{i:02d}:00+08:00") for i in range(10)}
    entries["a/b#99"] = candidate(99, "2026-11-12T01:00:00+08:00", "running")
    seed(queue_root, entries)
    monkeypatch.setattr(data, "screen_issue", lambda *a, **kw: ([], {"usable": True, "reason": "accepted"}))
    ds = SimpleNamespace(root=queue_root)
    assert data.ingest_cloudflare_issues(ds, [{"repo": "a/b", "number": 20}])[0] == 1  # B
    q = read_queue(queue_root)
    assert q["a/b#0"]["status"] == "queue_evicted"
    assert q["a/b#99"] == entries["a/b#99"]
    q["a/b#99"]["status"] = "pr_open"  # A finished
    write_queue(queue_root, q)
    now = dt.datetime.fromisoformat("2026-11-12T02:01:00+08:00")
    monkeypatch.setattr(state_store, "business_now", lambda *a: now)
    monkeypatch.setattr(pipeline, "business_now", lambda *a: now)
    assert data.ingest_cloudflare_issues(ds, [{"repo": "a/b", "number": 21}])[0] == 1  # C
    q = read_queue(queue_root)
    assert not q["a/b#21"].get("direct_start_reserved_at")
    picked = pipeline._dispatch_candidates(pipeline._order_candidates(list(q.items())))
    assert [key for key, _ in picked] == ["a/b#20", "a/b#21"]
    q["a/b#20"]["direct_start_reserved_at"] = now.isoformat()  # B selected, not yet on Actions
    write_queue(queue_root, q)
    assert data.ingest_cloudflare_issues(ds, [{"repo": "a/b", "number": 22}])[0] == 1  # D
    q = read_queue(queue_root)
    assert q["a/b#20"].get("direct_start_reserved_at")
    assert state_store.staged_candidate_count(q) == 10
    rows = {r["key"]: scan_row_to_fields(r) for r in build_scan_rows(queue_root)}
    assert rows["a/b#0"]["最终结果"].startswith("已清除 · 暂存满 10 条")
    assert rows["a/b#0"]["粗筛结果"] == "通过"
    assert rows["a/b#0"]["细筛结果"] == "verified"
    assert rows["a/b#0"]["更新时间"]
    assert "a/b#0" not in {r["key"] for r in build_candidate_rows(queue_root)}


def test_today_only_full_pool_also_evicts_oldest_and_rejections_never_evict(queue_root, monkeypatch):
    seed(queue_root, {f"a/b#{i}": candidate(i, f"2026-11-12T01:{i:02d}:00+08:00") for i in range(10)})
    ds = SimpleNamespace(root=queue_root)
    monkeypatch.setattr(data, "screen_issue", lambda *a, **kw: ([], {"usable": False}))
    before = read_queue(queue_root)
    assert data.ingest_cloudflare_issues(ds, [{"repo": "a/b", "number": 20}])[0] == 0
    assert read_queue(queue_root) == before
    monkeypatch.setattr(data, "screen_issue", lambda *a, **kw: ([], {"usable": True}))
    assert data.ingest_cloudflare_issues(ds, [{"repo": "a/b", "number": 21}])[0] == 1
    q = read_queue(queue_root)
    assert q["a/b#0"]["status"] == "queue_evicted"
    assert q["a/b#21"]["status"] == "staged"
    assert not q["a/b#21"].get("direct_start_reserved_at")
    assert state_store.staged_candidate_count(q) == 10


def test_backoff_does_not_block_a_new_direct_start(queue_root, monkeypatch):
    old = candidate(1, "2026-11-12T01:00:00+08:00")
    old["screen_retry_at"] = "2026-11-12T03:00:00+08:00"
    seed(queue_root, {old["id"]: old})
    monkeypatch.setattr(data, "screen_issue", lambda *a, **kw: ([], {"usable": True}))
    data.ingest_cloudflare_issues(SimpleNamespace(root=queue_root), [{"repo": "a/b", "number": 2}])
    assert read_queue(queue_root)["a/b#2"].get("direct_start_reserved_at")
