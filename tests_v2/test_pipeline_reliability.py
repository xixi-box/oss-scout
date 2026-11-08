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
    ds = SimpleNamespace(root=tmp_path, _cached=lambda k, ttl, fn: fn())
    issue = {"repo": "a/b", "number": 1, "title": "Parser returns wrong value",
             "body_excerpt": "parse('x') returns 2; expected 1"}
    return ds, issue


def test_short_precise_report_reaches_ai(intake):
    ds, issue = intake
    with patch("scout_v2.workers.openjev.OpenJevClient") as cls:
        cls.return_value.score_issue.return_value = {"usable": True, "reason": "repro"}
        assert d.ingest_cloudflare_issues(ds, [issue])[0] == 1


def test_ai_failure_is_retryable(intake):
    ds, issue = intake
    issue["body_excerpt"] *= 20
    with patch("scout_v2.workers.openjev.OpenJevClient") as cls:
        cls.return_value.score_issue.return_value = {"usable": None, "reason": "timeout"}
        assert d.ingest_cloudflare_issues(ds, [issue])[0] == 0
        cls.return_value.score_issue.return_value = {"usable": True, "reason": "repro"}
        assert d.ingest_cloudflare_issues(ds, [issue])[0] == 1


def test_mechanical_rejection_does_not_call_ai(intake):
    ds, issue = intake
    issue["labels"] = ["duplicate"]
    with patch("scout_v2.workers.openjev.OpenJevClient") as cls:
        d.ingest_cloudflare_issues(ds, [issue])
        cls.return_value.score_issue.assert_not_called()


def test_openjev_receives_actor_time_and_linked_pr_evidence(intake, monkeypatch):
    import io
    ds, issue = intake
    issue.update(context_complete=True, reporter="author", reporter_association="CONTRIBUTOR",
                 issue_comments=[{"author": "author", "association": "CONTRIBUTOR",
                                  "created_at": "2026-11-07T00:01:00Z", "body": "Reproduction added"}])
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setenv("TYPESAFE_BASE_URL", "https://api.codiv.ai")
    monkeypatch.setenv("TYPESAFE_MODEL", "openjev-latest")
    monkeypatch.setattr("scout_v2.git_host.gh_pages", lambda *a: [
        {"event": "cross-referenced", "source": {"issue": {
            "state": "open", "title": "Related PR", "html_url": "https://github.com/a/b/pull/8",
            "pull_request": {"url": "https://api.github.com/repos/a/b/pulls/8"},
            "user": {"login": "other"},
        }}},
    ])
    calls = []
    def request(req, timeout):
        calls.append(json.loads(req.data))
        return io.BytesIO(b'{"model":"openjev-0.1","answers":{"eligible":{"noul":0.8}}}')
    monkeypatch.setattr("scout_v2.workers.openjev.urllib.request.urlopen", request)
    assert d.ingest_cloudflare_issues(ds, [issue])[0] == 1
    assert d.ingest_cloudflare_issues(ds, [issue])[0] == 0
    assert len(calls) == 1
    evidence = calls[0]["state"]
    assert evidence["comments"][0]["is_reporter"]
    assert evidence["comments"][0]["created_at"] == "2026-11-07T00:01:00Z"
    assert evidence["linked_pull_requests"][0]["url"].endswith("/8")
    assert evidence["repo_rules"] == "test rules"


@pytest.mark.parametrize(("check_overrides", "reason"), [
    ({"unassigned": False}, "已有指派"),
    ({"is_duplicate": True}, "confirmed_duplicate"),
    ({"open_pr_hits": ["docker/compose#3"]}, "已有修复 PR · docker/compose#3"),
])
def test_prestart_coarse_blocker_stops_before_claim(tmp_path, monkeypatch, check_overrides, reason):
    (tmp_path / "queue").mkdir()
    (tmp_path / "state").mkdir()
    entry = {"id": "docker/compose#1", "repo": "docker/compose", "number": 1, "status": "new"}
    queue = tmp_path / "queue/candidates.json"
    queue.write_text(json.dumps({entry["id"]: entry}))
    monkeypatch.setattr(p.PolicyEngine, "load", lambda _: (
            SimpleNamespace(repo_lane=lambda _: "open", tz_name="Asia/Shanghai", max_claims_per_day=5,
                            repos_cfg={"repositories": {entry["repo"]: {}}}), None))
    monkeypatch.setattr(p, "gh_read", lambda args: {"state": "active"})
    check = dict(complete=True, issue_open=True, unassigned=True, has_claim_signal=False,
                 is_duplicate=False, has_cross_ref_pr=False, open_pr_hits=[])
    monkeypatch.setattr(p, "recheck_candidate", lambda *a, **kw: SimpleNamespace(**(check | check_overrides)))
    out = p.candidate_pipeline(tmp_path, entry)
    assert reason in out["abort"]
    saved = json.loads(queue.read_text(encoding="utf-8"))[entry["id"]]
    assert saved["status"] == "coarse_blocked"
    assert saved["coarse_reason"] == reason


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
        if "/timeline?" in path:
            return []
        if path.startswith("search/"):
            return {"items": []}
        if "/comments?" not in path:
            return {"state": "open", "body": "latest", "labels": []}
        if "page=2" in path:
            return [{"body": "maintainer says stop", "user": {"login": "maintainer"}}]
        return [{"body": "earlier", "user": {"login": "reporter"}}] * 100
    monkeypatch.setattr("scout_v2.git_host.gh_read", read)
    fresh, blockers, timeline = d.refresh_issue({"repo": "a/b", "number": 1})
    assert blockers == timeline == []
    assert sum("/timeline?" in path for path in calls) == 1
    assert fresh["comments"] == 101
    assert fresh["issue_comments"][-1]["body"] == "maintainer says stop"


def test_live_intake_blocks_claim_before_openjev(intake, monkeypatch):
    ds, issue = intake
    monkeypatch.setattr("scout_v2.git_host.recheck_candidate", lambda *a, **kw: SimpleNamespace(
        complete=True, issue_open=True, unassigned=True, has_claim_signal=True,
        has_cross_ref_pr=False, open_pr_hits=[],
        is_duplicate=False, screening_source=({"title": issue["title"], "state": "open"}, [], [])))

    def unexpected_score(*args, **kwargs):
        pytest.fail("a claimed issue must never reach OpenJev")

    monkeypatch.setattr("scout_v2.workers.openjev.OpenJevClient.score_issue", unexpected_score)
    assert d.ingest_cloudflare_issues(ds, [issue], refresh=True) == (0, [])
    seen = json.loads((ds.root / "state/seen.json").read_text())
    assert seen["a/b#1"]["coarse_reason"] == "claim_signal"


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


@pytest.mark.parametrize("mode", ["fresh", "immediate", "staged", "manual", "fresh_blocked", "staged_blocked"])
def test_failed_repair_does_not_spend_publication_quota(tmp_path, monkeypatch, mode):
    (tmp_path / "queue").mkdir()
    (tmp_path / "state").mkdir()
    entries = {f"docker/compose#{i}": {"id": f"docker/compose#{i}", "repo": "docker/compose", "number": i,
                            "title": "Parser bug", "status": "new", "context_complete": True,
                            "ai_verdict": {"usable": True, "provider": "codiv"}}
               for i in range(1, 8)}
    if mode.startswith("staged"):
        from scout_v2.state_store import business_now
        now = business_now()
        for i in (4, 5, 6):
            entries[f"docker/compose#{i}"].update(
                status="staged", staged_at=(now - dt.timedelta(days=2)).isoformat(),
                eligible_at=dt.datetime.combine(now.date() - dt.timedelta(days=1),
                                                dt.time(4), tzinfo=now.tzinfo).isoformat())
    (tmp_path / "queue/candidates.json").write_text(json.dumps(entries))
    eng = SimpleNamespace(repo_lane=lambda _: "open", tz_name="Asia/Shanghai",
                          max_claims_per_day=5, snapshot_id=lambda: "test",
                          repos_cfg={"repositories": {"docker/compose": {}}})
    monkeypatch.setattr(p.PolicyEngine, "load", lambda _: (eng, None))
    rechecked = []

    def recheck(repo, number, **kwargs):
        rechecked.append(number)
        return SimpleNamespace(
            complete=True, issue_open=True, unassigned=True, has_claim_signal=False,
            has_cross_ref_pr=False, open_pr_hits=[], is_duplicate=False,
            screening_source=({"title": "Parser bug", "state": "open", "labels": (
                [{"name": "documentation"}] if mode.endswith("_blocked") and number == 4 else []
            )}, [], []))

    monkeypatch.setattr(p, "recheck_candidate", recheck)
    monkeypatch.setattr("scout_v2.workers.cla.preflight", lambda *a: {"blocking": False})
    monkeypatch.setattr("scout_v2.workers.repair.resolve_model", lambda: "test")
    screens = []
    monkeypatch.setattr("scout_v2.workers.openjev.OpenJevClient.score_issue",
                        lambda self, issue: screens.append(issue["number"]) or {"usable": True})
    monkeypatch.setattr(p.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=0, stdout="a" * 40, stderr=""))
    monkeypatch.setattr(p, "_build_router", lambda *a: SimpleNamespace(
        route=lambda job: SimpleNamespace(backend="github_actions")))
    from scout_v2.state_store import business_now
    submitted = []

    def submit(_root, job, _eng):
        submitted.append(job)
        return "github_actions", str(len(submitted)), {}

    def gh_read(args):
        path = args[0]
        if "/workflows/" in path:
            return {"state": "active"}
        job = submitted[-1]
        if "/runs/" in path:
            return {"display_title": f"repair {job.job_id}", "status": "completed"}
        if len(submitted) == 1:
            return []
        return [{"html_url": f"https://github.com/docker/compose/pull/{len(submitted)}",
                 "body": job.publish["body"], "user": {"login": "xixi-box"},
                 "created_at": business_now().isoformat()}]

    monkeypatch.setattr(p, "gh_read", gh_read)
    monkeypatch.setattr(p, "_submit_job", submit)
    monkeypatch.setattr(p, "_collect", lambda *args: {})
    monkeypatch.setattr("scout_v2.notify.feishu_send", lambda *a, **kw: None)
    monkeypatch.setattr("scout_v2.workers.cla.sign", lambda *a: {})
    for entry in entries.values():
        p.candidate_pipeline(tmp_path, entry, rescreen=mode == "manual",
                             immediate=mode == "immediate" and entry["number"] == 1)
    if mode in {"fresh", "immediate"}:
        assert rechecked == (list(range(2, 7)) if mode == "immediate" else list(range(1, 7)))
    led = ClaimLedger(tmp_path / "state/scout_v2.sqlite3")
    assert led.count_today("Asia/Shanghai") == 6
    assert led.published_today("Asia/Shanghai") == 5
    led.close()
    assert len(submitted) == 6
    assert screens == {"fresh": [], "immediate": [], "staged": [4, 5, 6], "manual": [1, 2, 3, 4, 5, 6],
                       "fresh_blocked": [], "staged_blocked": [5, 6]}[mode]
    q = json.loads((tmp_path / "queue/candidates.json").read_text())
    if mode.endswith("_blocked"):
        assert q["docker/compose#4"]["blockers"] == ["excluded_label"]
        assert q["docker/compose#7"]["status"] == "pr_open"
    else:
        assert q["docker/compose#7"]["status"] == "staged"


def test_publication_reconciled_on_github_creation_day(tmp_path, monkeypatch):
    from scout_v2 import state_store
    ledger = ClaimLedger(tmp_path / "scout.sqlite3")
    tz = "Asia/Shanghai"
    monkeypatch.setattr(state_store, "business_now",
                        lambda _: dt.datetime.fromisoformat("2026-11-08T00:05:00+08:00"))
    assert ledger.reserve_publication(tz, 5, "test", "docker/compose", "xixi-box:fix", "xixi-box/oss-scout")
    assert not ledger.reserve_publication(tz, 5, "other", "docker/compose", "xixi-box:other", "xixi-box/oss-scout")
    ledger.set_publication_run("test", "1")
    responses = iter([
        {"status": "completed", "display_title": "repair test"},
        [{"body": "<!-- oss-scout-job:test -->", "created_at": "2026-11-07T16:01:00Z",
          "user": {"login": "xixi-box"}, "html_url": "https://github.com/docker/compose/pull/1"}],
    ])
    monkeypatch.setattr(p, "gh_read", lambda args: next(responses))
    assert p._reconcile_publication(ledger, tz)
    assert ledger.pending_publication() is None
    assert ledger.published_today(tz) == 1
    ledger.close()


@pytest.mark.parametrize("published", [False, True])
def test_controller_restart_recovers_candidate_and_feishu_status(tmp_path, monkeypatch, published):
    from scripts.monitor_prs import build_candidate_rows, build_scan_rows

    key = "nodejs/node#67320"
    job = "job-123"
    url = "https://github.com/nodejs/node/pull/67399"
    (tmp_path / "queue").mkdir()
    queue = tmp_path / "queue/candidates.json"
    queue.write_text(json.dumps({key: {"status": "escalated", "title": "Fix test runner"}}))
    ledger = ClaimLedger(tmp_path / "state/scout_v2.sqlite3")
    tz = "Asia/Shanghai"
    (tmp_path / "state/seen.json").write_text(json.dumps({key: {"status": "ai_passed"}}))
    assert ledger.reserve_publication(tz, 5, job, "nodejs/node",
                                      "xixi-box:fix/issue-67320-5f602d5-ab588978", "xixi-box/oss-scout")
    ledger.close()
    run = {"id": 703, "display_title": f"repair {job}", "status": "in_progress"}
    created = p.business_now(tz).isoformat()
    prs = [{"body": f"<!-- oss-scout-job:{job} -->", "created_at": created,
            "user": {"login": "xixi-box"}, "html_url": url}] if published else []

    def gh_read(args):
        if "/workflows/" in args[0]:
            return {"workflow_runs": [run]}
        return prs if args[0].endswith("/pulls") else run

    monkeypatch.setattr(p, "gh_read", gh_read)
    result = {"outcome": "inconclusive", "unverified_items": ["no artifacts and logs unavailable"]}
    monkeypatch.setattr(p.ActionsBackend, "collect", lambda self, run_id: result)
    assert not p.recover_pending_publication(tmp_path)
    assert json.loads(queue.read_text(encoding="utf-8"))[key]["status"] == "escalated"
    run.update(status="completed", conclusion="success")
    if not published:
        assert not p.recover_pending_publication(tmp_path)
        result.update(job_id=job, outcome="failed", unverified_items=["Only tests changed"])

    assert p.recover_pending_publication(tmp_path)
    entry = json.loads(queue.read_text(encoding="utf-8"))[key]
    assert entry["status"] == ("pr_open" if published else "aborted_no_evidence")
    assert entry["repair_run_id"] == "703"
    assert entry["workflow_conclusion"] == "success"
    assert (entry["pr_url"] == url if published else "Only tests changed" in entry["abort_reason"])
    assert build_candidate_rows(tmp_path, {key}) == []
    scan, = build_scan_rows(tmp_path, {key})
    if published:
        assert scan["pr_url"] == url
    else:
        assert "Only tests changed" in scan["final_result"]
    ledger = ClaimLedger(tmp_path / "state/scout_v2.sqlite3")
    assert ledger.pending_publication() is None
    assert ledger.published_today(tz) == int(published)
    ledger.close()
    assert not p.recover_pending_publication(tmp_path)


def test_same_day_pr_closure_refills_one_slot_without_rewriting_queue(tmp_path, monkeypatch):
    from scout_v2 import state_store
    from scout_v2.feishu_bitable import scan_row_to_fields
    from scripts.monitor_prs import build_scan_rows

    now = dt.datetime.fromisoformat("2026-11-08T07:00:00+08:00")
    day = now.date().isoformat()
    tz = "Asia/Shanghai"
    monkeypatch.setattr(p, "business_now", lambda *args: now)
    monkeypatch.setattr(state_store, "business_now", lambda *args: now)
    ledger = ClaimLedger(tmp_path / "state/scout_v2.sqlite3")
    repo = "docker/compose"
    closed = f"https://github.com/{repo}/pull/101"
    merged = f"https://github.com/{repo}/pull/102"
    old = f"https://github.com/{repo}/pull/100"
    for job, url, created_day in [("old", old, "2026-11-07"), ("one", closed, day), ("two", merged, day)]:
        assert ledger.reserve_publication(tz, 2, job, repo, "head", "xixi-box/oss-scout")
        ledger.finish_publication(job, day=created_day, url=url)
    assert not ledger.reserve_publication(tz, 2, "full", repo, "head", "xixi-box/oss-scout")

    queue = {}
    for number in (1, 2):
        key = f"{repo}#{number}"
        entry = {"id": key, "repo": repo, "number": number, "status": "new",
                 "discovered_at": f"{day}T06:0{number}:00+08:00", "ai_verdict": {"usable": True}}
        p._defer_entry(entry, tmp_path)
        queue[key] = entry
    (tmp_path / "queue").mkdir()
    path = tmp_path / "queue/candidates.json"
    path.write_text(json.dumps(queue))
    assert p.drain_ai_graded(tmp_path, limit=1) == []

    replies = {
        f"repos/{repo}/pulls/101": {"html_url": closed, "state": "closed", "merged": False,
                                   "closed_at": "2026-11-07T22:59:00Z"},
        f"repos/{repo}/pulls/102": {"html_url": merged, "state": "closed", "merged": True},
    }
    checked = []

    def gh_read(args, **kwargs):
        checked.append(args[0])
        return replies[args[0]]

    monkeypatch.setattr(p, "gh_read", gh_read)
    assert p.refresh_publication_status(tmp_path)
    assert checked == list(replies)
    assert not p.refresh_publication_status(tmp_path)
    assert ledger.published_today(tz) == 2
    assert ledger.quota_used(day) == 1
    assert [row["id"] for row in p.drain_ai_graded(tmp_path, limit=1)] == [f"{repo}#1"]
    assert json.loads(path.read_text()) == queue
    assert p._order_candidates(list(queue.items()), refill_day="2026-11-07") == []
    (tmp_path / "state/seen.json").write_text(json.dumps({f"{repo}#1": {}}))
    row, = build_scan_rows(tmp_path, published={f"{repo}#1": closed}, pr_states={closed: "closed"})
    assert scan_row_to_fields(row)["最终结果"] == [
        {"type": "url", "text": f"PR 已关闭 · {repo}#101", "link": closed}]

    assert ledger.reserve_publication(tz, 2, "refill", repo, "head", "xixi-box/oss-scout")
    replacement = f"https://github.com/{repo}/pull/103"
    ledger.finish_publication("refill", day=day, url=replacement)
    assert ledger.published_today(tz) == 3
    assert not ledger.reserve_publication(tz, 2, "still-full", repo, "head", "xixi-box/oss-scout")
    replies[f"repos/{repo}/pulls/101"].update(state="open", closed_at=None)
    replies[f"repos/{repo}/pulls/103"] = None
    assert p.refresh_publication_status(tmp_path)
    assert ledger.quota_used(day) == 3
    assert not ledger.has_publication_release(day)
    assert ledger.quota_used("2026-11-09") == 0
    ledger.close()


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
    with patch("scout_v2.workers.openjev.OpenJevClient.score_issue") as score:
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


def test_table_does_not_hide_a_live_staged_candidate_due_to_old_update_time():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "scripts/monitor_prs.py"
    spec = importlib.util.spec_from_file_location("monitor_table_regression", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.queue_display_state("staged", "2020-01-01T00:00:00") == "排队中"


@pytest.mark.parametrize("hour,minute", [(0, 0), (1, 0), (3, 59)])
def test_fresh_issue_consumes_normally_before_four_am_with_staged_backlog(monkeypatch, hour, minute):
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(p, "business_now", lambda *a: dt.datetime(2026, 11, 7, hour, minute,
                                                                tzinfo=ZoneInfo("Asia/Shanghai")))
    old = {"status": "staged", "staged_at": "2026-11-06T20:00:00+08:00",
           "eligible_at": "2026-11-07T04:00:00+08:00"}
    new = {"status": "new", "discovered_at": f"2026-11-07T{hour:02d}:{minute:02d}:00+08:00"}
    assert p._order_candidates([("old", old), ("new", new)]) == [("new", new)]
