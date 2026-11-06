"""Regression coverage for ingestion, queue ordering and daily consumption."""
import datetime as dt
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from dashboard import data as d
from scout_v2.workers import auto_pipeline as p
from scout_v2.state_store import ClaimLedger, business_day


@pytest.fixture
def intake(tmp_path, monkeypatch):
    (tmp_path / "queue").mkdir()
    (tmp_path / "state").mkdir()
    (tmp_path / "queue/candidates.json").write_text("{}")
    eng = SimpleNamespace(repos_cfg={"repositories": {"a/b": {}}},
                          repo_lane=lambda _: "open")
    monkeypatch.setattr(p.PolicyEngine, "load", lambda _: (eng, None))
    monkeypatch.setattr(d, "repo_rules_text", lambda _: "test rules")
    monkeypatch.setattr("scout_v2.workers.issue_scorer.load_scorer_config",
                        lambda _: {"api_key": "fake"})
    ds = SimpleNamespace(root=tmp_path, _cached=lambda k, ttl, fn: fn())
    issue = {"repo": "a/b", "number": 1, "title": "Parser returns wrong value",
             "body_excerpt": "parse('x') returns 2; expected 1"}
    return ds, issue


def test_short_precise_report_reaches_ai(intake):
    ds, issue = intake
    with patch("scout_v2.workers.issue_scorer.GoClient") as cls:
        cls.return_value.score_issue.return_value = {"usable": True, "reason": "repro"}
        assert d.ingest_cloudflare_issues(ds, [issue])[0] == 1


def test_ai_failure_is_retryable(intake):
    ds, issue = intake
    issue["body_excerpt"] *= 20
    with patch("scout_v2.workers.issue_scorer.GoClient") as cls:
        cls.return_value.score_issue.return_value = {"usable": None, "reason": "timeout"}
        assert d.ingest_cloudflare_issues(ds, [issue])[0] == 0
        cls.return_value.score_issue.return_value = {"usable": True, "reason": "repro"}
        assert d.ingest_cloudflare_issues(ds, [issue])[0] == 1


def test_mechanical_rejection_does_not_call_ai(intake):
    ds, issue = intake
    issue["labels"] = ["duplicate"]
    with patch("scout_v2.workers.issue_scorer.GoClient") as cls:
        d.ingest_cloudflare_issues(ds, [issue])
        cls.return_value.score_issue.assert_not_called()


def test_competed_candidate_exits_without_uninitialized_ledger(tmp_path, monkeypatch):
    (tmp_path / "queue").mkdir()
    (tmp_path / "state").mkdir()
    (tmp_path / "queue/candidates.json").write_text("{}")
    monkeypatch.setattr(p.PolicyEngine, "load", lambda _: (
        SimpleNamespace(repo_lane=lambda _: "open"), None))
    monkeypatch.setattr(p, "recheck_candidate", lambda *a, **kw: SimpleNamespace(
        complete=True, issue_open=True, unassigned=False, has_claim_signal=False,
        has_cross_ref_pr=False, open_pr_hits=[]))
    out = p.candidate_pipeline(tmp_path, {"id": "a/b#1", "repo": "a/b", "number": 1})
    assert "competed" in out["abort"]


def test_abort_keeps_consumed_daily_slot(tmp_path):
    led = ClaimLedger(tmp_path / "state/scout_v2.sqlite3")
    assert led.try_claim("Asia/Shanghai", "a/b#1", max_per_day=5)[0]
    p._abort_release_impl("backend failed", candidate_id="a/b#1", ledger=led,
                         eng=SimpleNamespace(tz_name="Asia/Shanghai"), summary={},
                         log_fn=lambda *a: None, root=tmp_path, key="a/b#1")
    check = ClaimLedger(tmp_path / "state/scout_v2.sqlite3")
    assert check.count_today("Asia/Shanghai") == 1
    check.close()


def test_staged_is_not_consumed_before_next_four_am():
    from zoneinfo import ZoneInfo
    now = dt.datetime.now(ZoneInfo("Asia/Shanghai"))
    future = (now + dt.timedelta(days=1)).isoformat()
    entries = [("a/b#1", {"status": "staged", "staged_at": now.isoformat(),
                           "eligible_at": future})]
    assert p._order_candidates(entries) == []


def test_queue_updates_do_not_lose_other_threads(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    (tmp_path / "queue").mkdir()
    entries = {f"a/b#{i}": {"status": "new"} for i in range(50)}
    qp = tmp_path / "queue/candidates.json"
    qp.write_text(json.dumps(entries))
    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(lambda k: p.mark_candidate(tmp_path, k, "pr_open"), entries))
    assert all(e["status"] == "pr_open" for e in json.loads(qp.read_text()).values())


def test_inbox_survives_restart_and_preserves_newer_delivery(tmp_path):
    from scout_v2.state_store import IssueInbox
    box = IssueInbox(tmp_path)
    box.put([{"repo": "a/b", "number": 1, "title": "first"}])
    box.close()
    box = IssueInbox(tmp_path)
    key, payload, version, attempts = box.take()
    box.put([{"repo": "a/b", "number": 1, "title": "updated"}])
    box.finish(key, version)
    next_item = box.take()
    assert json.loads(next_item[1])["title"] == "updated"
    box.finish(next_item[0], next_item[2])
    assert box.take() is None
    box.close()


def test_staging_keeps_original_due_time(tmp_path, monkeypatch):
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(p, "business_now", lambda *a: dt.datetime(2026, 11, 6, 23, 59,
                                                                tzinfo=ZoneInfo("Asia/Shanghai")))
    entry = {"status": "new"}
    p._defer_entry(entry, tmp_path)
    assert entry["eligible_at"] == "2026-11-07T04:00:00+08:00"
    p._defer_entry(entry, tmp_path)
    assert entry["staged_at"] == "2026-11-06T23:59:00+08:00"


def test_staged_boundary_and_fifo(tmp_path, monkeypatch):
    from zoneinfo import ZoneInfo
    entries = [("second", {"status": "staged", "staged_at": "2026-11-06T23:00:00+08:00",
                            "eligible_at": "2026-11-07T04:00:00+08:00"}),
               ("first", {"status": "staged", "staged_at": "2026-11-06T20:00:00+08:00",
                           "eligible_at": "2026-11-07T04:00:00+08:00"})]
    monkeypatch.setattr(p, "business_now", lambda *a: dt.datetime(2026, 11, 7, 3, 59,
                                                                tzinfo=ZoneInfo("Asia/Shanghai")))
    assert p._order_candidates(entries) == []
    monkeypatch.setattr(p, "business_now", lambda *a: dt.datetime(2026, 11, 7, 4, 0,
                                                                tzinfo=ZoneInfo("Asia/Shanghai")))
    assert [key for key, _ in p._order_candidates(entries)] == ["first", "second"]


def test_refresh_fetches_every_comment(monkeypatch):
    calls = []
    def read(args):
        path = args[0]
        calls.append(path)
        if "/comments?" not in path:
            return {"state": "open", "body": "latest", "labels": []}
        if "page=2" in path:
            return [{"body": "maintainer says stop", "user": {"login": "maintainer"}}]
        return [{"body": "earlier", "user": {"login": "reporter"}}] * 100
    monkeypatch.setattr("scout_v2.git_host.gh_read", read)
    fresh = d.refresh_issue({"repo": "a/b", "number": 1})
    assert fresh["comments"] == 101
    assert fresh["issue_comments"][-1]["body"] == "maintainer says stop"


def test_default_ledger_time_uses_business_timezone(tmp_path, monkeypatch):
    import scout_v2.state_store as st
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(st, "business_now", lambda *a: dt.datetime(2026, 11, 7, 0, 30,
                                                                 tzinfo=ZoneInfo("Asia/Shanghai")))
    led = ClaimLedger(tmp_path / "test.db")
    assert led.try_claim("Asia/Shanghai", "a/b#1", max_per_day=5)[0]
    assert led.claimed_ids("2026-11-07") == ["a/b#1"]
    led.close()


def test_completed_inbox_payload_is_not_refetched(tmp_path):
    from scout_v2.state_store import IssueInbox
    box = IssueInbox(tmp_path)
    issue = {"repo": "a/b", "number": 1, "updated_at": "same"}
    box.put([issue])
    row = box.take()
    box.finish(row[0], row[2])
    box.put([issue])
    assert box.take() is None
    box.close()


def test_unconsumed_backlog_waits_for_next_four_am(tmp_path, monkeypatch):
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(p, "business_now", lambda *a: dt.datetime(2026, 11, 7, 18, 0,
                                                                tzinfo=ZoneInfo("Asia/Shanghai")))
    entry = {"status": "staged", "staged_at": "2026-11-06T20:00:00+08:00",
             "eligible_at": "2026-11-07T04:00:00+08:00"}
    p._defer_entry(entry, tmp_path)
    assert entry["eligible_at"] == "2026-11-08T04:00:00+08:00"
    assert entry["staged_at"] == "2026-11-06T20:00:00+08:00"


def test_failed_repairs_still_stop_at_five_starts(tmp_path, monkeypatch):
    (tmp_path / "queue").mkdir()
    (tmp_path / "state").mkdir()
    entries = {f"a/b#{i}": {"id": f"a/b#{i}", "repo": "a/b", "number": i,
                            "title": "Parser bug", "status": "new", "context_complete": True}
               for i in range(1, 7)}
    (tmp_path / "queue/candidates.json").write_text(json.dumps(entries))
    eng = SimpleNamespace(repo_lane=lambda _: "open", tz_name="Asia/Shanghai",
                          max_claims_per_day=5, snapshot_id=lambda: "test")
    monkeypatch.setattr(p.PolicyEngine, "load", lambda _: (eng, None))
    monkeypatch.setattr(p, "recheck_candidate", lambda *a, **kw: SimpleNamespace(
        complete=True, issue_open=True, unassigned=True, has_claim_signal=False,
        has_cross_ref_pr=False, open_pr_hits=[], is_duplicate=False))
    monkeypatch.setattr("scout_v2.workers.cla.preflight", lambda *a: {"blocking": False})
    monkeypatch.setattr("scout_v2.workers.repair.resolve_model", lambda: "test")
    monkeypatch.setattr(d, "refresh_issue", lambda entry: {**entry, "comments": 0, "issue_comments": []})
    monkeypatch.setattr("scout_v2.workers.issue_scorer.load_scorer_config",
                        lambda root: {"api_key": "fake", "model": "test"})
    monkeypatch.setattr("scout_v2.workers.issue_scorer.GoClient.score_issue",
                        lambda self, issue: {"usable": True, "reason": "reproduced"})
    monkeypatch.setattr(p.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=0, stdout="a" * 40, stderr=""))
    monkeypatch.setattr(p, "_build_router", lambda *a: SimpleNamespace(
        route=lambda job: SimpleNamespace(backend="github_actions")))
    submitted = []
    def unavailable(*args):
        submitted.append(args[1].repo)
        raise RuntimeError("backend unavailable")
    monkeypatch.setattr(p, "_submit_job", unavailable)
    for entry in entries.values():
        p.candidate_pipeline(tmp_path, entry)
    led = ClaimLedger(tmp_path / "state/scout_v2.sqlite3")
    assert led.count_today("Asia/Shanghai") == 5
    led.close()
    assert len(submitted) == 5
    q = json.loads((tmp_path / "queue/candidates.json").read_text())
    assert q["a/b#6"]["status"] == "staged"


def test_http_ingest_durably_accepts_entire_batch(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from dashboard.app import create_app
    from scout_v2.state_store import IssueInbox
    monkeypatch.setenv("OSSCOUT_DISABLE_AUTO_REPAIR", "1")
    (tmp_path / "state").mkdir()
    (tmp_path / "queue").mkdir()
    monkeypatch.setattr(d, "cloudflare_cfg", lambda ds: {"ingest_token": "test"})
    app = create_app(tmp_path, auth_enabled=False, password="test")
    issues = [{"repo": "a/b", "number": i, "body_excerpt": "x" * 16000,
               "issue_comments": [{"author": "maintainer", "body": "stop"}]}
              for i in range(1, 202)]
    with patch("scout_v2.workers.issue_scorer.GoClient.score_issue") as score:
        response = TestClient(app).post("/api/ingest/issues", json={"issues": issues},
                                       headers={"X-Ingest-Token": "test"})
        assert response.status_code == 200
        assert response.json()["stored"] == 201
        score.assert_not_called()
    box = IssueInbox(tmp_path)
    row = box.take()
    assert len(json.loads(row[1])["body_excerpt"]) == 16000
    assert json.loads(row[1])["issue_comments"][0]["body"] == "stop"
    box.close()


def test_quota_migration_counts_previously_refunded_starts_once(tmp_path):
    from scout_v2.state_store import business_now
    ts = business_now().isoformat()
    log = tmp_path / "worklog.jsonl"
    log.write_text("\n".join(json.dumps({"action": "claim", "target": key, "ts": ts})
                             for key in ["a/b#1", "a/b#1", "a/b#2"]))
    led = ClaimLedger(tmp_path / "ledger.db")
    led.try_claim("Asia/Shanghai", "a/b#1", max_per_day=5)
    led.reconcile_legacy_starts(log, "Asia/Shanghai")
    assert led.count_today("Asia/Shanghai") == 3
    led.reconcile_legacy_starts(log, "Asia/Shanghai")
    assert led.count_today("Asia/Shanghai") == 3
    led.close()


def test_newer_payload_does_not_inherit_older_failure_backoff(tmp_path):
    from scout_v2.state_store import IssueInbox
    box = IssueInbox(tmp_path)
    box.put([{"repo": "a/b", "number": 1, "title": "old"}])
    row = box.take()
    box.put([{"repo": "a/b", "number": 1, "title": "new"}])
    box.finish(row[0], row[2], "old request failed", attempts=5)
    assert json.loads(box.take()[1])["title"] == "new"
    box.close()


def test_other_process_dispatch_lock_prevents_duplicate_work(tmp_path):
    import sqlite3
    (tmp_path / "state").mkdir()
    conn = sqlite3.connect(tmp_path / "state/dispatcher.sqlite3", isolation_level=None)
    conn.execute("BEGIN IMMEDIATE")
    with patch.object(p, "recheck_candidate") as recheck:
        assert p.candidate_pipeline(tmp_path, {"id": "a/b#1"})["abort"] == "dispatcher busy"
        recheck.assert_not_called()
    conn.close()


def test_exact_roster_is_preserved():
    import re
    import yaml
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    repos = yaml.safe_load((root / "configs/repositories.yaml").read_text(encoding="utf-8"))
    monitored = {k for k, v in repos["repositories"].items() if not v.get("internal")}
    worker = re.search(r'REPOS = "([^"]+)"', (root / "cloudflare/wrangler.toml").read_text(encoding="utf-8"))[1].split(",")
    assert len(monitored) == 50
    assert set(worker) == monitored
