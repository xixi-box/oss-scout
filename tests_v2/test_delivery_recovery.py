"""Retained repairs require a new job and a real independent replay, never old success flags."""
import datetime as dt
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from scout_v2.schemas import Job
from scout_v2.workers import delivery_recovery as recovery, publication_guard, repair, core
from scout_v2.workers import publish_remote, delivery_review
from test_delivery_quality import delivered
from scout_v2.state_store import ClaimLedger, business_now
from scout_v2.workers import auto_pipeline as pipeline


def archive(delivered, tmp_path):
    repo, out, payload, _ = delivered
    payload["source_run"] = "12345"
    payload["runner_repo"] = "xixi-box/oss-scout"
    payload["publish"]["branch"] = "fix/issue-123-abcdef0-12345678"
    publication = {"ok": False, "failure_kind": "publication_rate_limited",
                   "error": "pr create failed: GraphQL: API rate limit already exceeded for user ID 96341168."}
    saved = publication_guard.preserve_verified_bundle(payload, out, publication)
    assert saved["ok"], saved
    source = {"job_id": payload["job_id"], "run_id": "12345", "repository": "xixi-box/oss-scout",
              "patch_sha256": saved["patch_sha256"], "base_sha": payload["base_sha"]}
    root = tmp_path / "controller"
    target = root / "artifacts/verified-deliveries" / payload["job_id"]
    target.parent.mkdir(parents=True)
    shutil.copytree(saved["path"], target)
    return repo, out, payload, source, root, target, publication


def test_retained_repair_replays_original_and_patch_without_another_implementation(delivered, tmp_path, monkeypatch):
    repo, out, payload, source, _, folder, _ = archive(delivered, tmp_path)
    job = Job.from_dict({**payload, "job_id": "job-new-reservation", "verified_delivery_source": source})
    monkeypatch.setenv("OSSCOUT_VERIFIED_DELIVERY_DIR", str(folder))
    monkeypatch.setattr(repair, "opencode_patch_producer", lambda *_a, **_k: pytest.fail("must reuse saved repair"))
    calls = []
    replay = repair._replay_regression
    def tracked(cmd, plan, work, env, dest, stage, **kwargs):
        calls.append(stage)
        return replay(cmd, plan, work, env, dest, stage, **kwargs)
    monkeypatch.setattr(repair, "_replay_regression", tracked)
    dest = tmp_path / "recovered"
    result = core.execute(job, dest, backend="github_actions", repo_dir=repo)
    assert result.outcome == "succeeded", result.unverified_items
    assert calls == ["base", "patched"]
    report = json.loads((dest / "test-report.json").read_text())
    assert report["job_id"] == job.job_id
    assert report["base_returncode"] == 1 and report["patched_returncode"] == 0
    assert hashlib.sha256((dest / "change.patch").read_bytes()).hexdigest() == source["patch_sha256"]
    assert report["delivery_preflight"]["recovered_from"] == source["job_id"]
    assert not delivery_review.review_approved(job.to_dict(), dest, {"ok": True})


@pytest.mark.parametrize("tamper", ["patch", "target", "base", "source_run", "extra_file"])
def test_retained_bundle_rejects_lineage_and_content_changes(delivered, tmp_path, tamper):
    _, _, payload, source, _, folder, _ = archive(delivered, tmp_path)
    repo, base = payload["repo"], payload["base_sha"]
    if tamper == "patch":
        (folder / "change.patch").write_text("tampered")
    elif tamper == "target":
        repo = "other/repo"
    elif tamper == "base":
        base = "a" * 40
    elif tamper == "source_run":
        source["run_id"] = "6789"
    else:
        (folder / "credentials.json").write_text("must never be consumed")
    with pytest.raises(ValueError):
        recovery.load_bundle(folder, source, repo=repo, base_sha=base, issue_number=123)


def test_publication_retry_is_delayed_bounded_and_requires_a_verified_saved_bundle(delivered, tmp_path):
    _, _, payload, _, root, _, publication = archive(delivered, tmp_path)
    now = dt.datetime(2026, 11, 17, 10, tzinfo=dt.timezone(dt.timedelta(hours=8)))
    kwargs = dict(job_id=payload["job_id"], repo=payload["repo"], issue_number=123,
                  run_id="12345", repository="xixi-box/oss-scout", now=now)
    fields = recovery.publication_retry_fields(root, {}, publication, **kwargs)
    assert fields["publication_retry_count"] == 1
    assert fields["retry_authorized_job_id"] == payload["job_id"]
    assert dt.datetime.fromisoformat(fields["eligible_at"]) == now + dt.timedelta(hours=1)
    repeated = recovery.publication_retry_fields(root, fields, publication, **{**kwargs, "now": now + dt.timedelta(minutes=10)})
    assert "eligible_at" not in repeated and "publication_retry_count" not in repeated
    assert not recovery.publication_retry_fields(root, {"publication_retry_count": 3}, publication, **kwargs)
    for unsafe in ({**publication, "created": True}, {**publication, "pr": "https://github.com/a/b/pull/1"},
                   {**publication, "failure_kind": "publication_permission_denied"},
                   {**publication, "failure_kind": "code_review_required"}):
        assert not recovery.publication_retry_fields(root, {}, unsafe, **kwargs)
    (root / "artifacts/verified-deliveries" / payload["job_id"] / "change.patch").write_text("changed")
    assert not recovery.publication_retry_fields(root, {}, publication, **kwargs)


def test_readonly_review_transport_error_is_distinct_from_real_findings(delivered, monkeypatch):
    repo, out, payload, _ = delivered
    evidence = {"ok": True, "snapshot": {"issue": {"state": "open"}, "comments": []}}
    class Unavailable:
        def run(self, *_):
            return False, "Codex temporary communication failure: operation=acquire; kind=TimeoutError"
    review = delivery_review.review_delivery(payload, repo, out, evidence, client=Unavailable())
    assert review["failure_kind"] == "review_communication_unavailable"
    monkeypatch.setattr(publish_remote, "refresh_publication_evidence", lambda *_: evidence)
    monkeypatch.setattr(publish_remote, "publish", lambda *_: pytest.fail("must not publish without review"))
    assert publish_remote.publish_from_env(payload, repo, out)["failure_kind"] == "review_communication_unavailable"


def test_today_docling_create_rate_limit_is_not_repeated_in_the_same_job(delivered, monkeypatch):
    repo, out, payload, _ = delivered
    evidence = {"ok": True, "snapshot": {"issue": {"state": "open"}, "comments": []}}
    class Approve:
        def run(self, *_):
            return True, '{"verdict":"approve","findings":[]}'
    delivery_review.review_delivery(payload, repo, out, evidence, client=Approve())
    monkeypatch.setattr(publish_remote, "refresh_publication_evidence", lambda *_: evidence)
    calls = []
    def limited(*_):
        calls.append(1)
        return {"ok": False, "error": "pr create failed: GraphQL: API rate limit already exceeded for user ID 96341168."}
    monkeypatch.setattr(publish_remote, "publish", limited)
    publication = publish_remote.publish_from_env(payload, repo, out)
    assert publication["failure_kind"] == "publication_rate_limited"
    assert publication["retry_after_seconds"] == 3600 and calls == [1]
    assert (out / "verified-delivery/change.patch").exists()


@pytest.mark.parametrize("pool_full", [False, True])
def test_completed_unpublished_rate_limit_releases_reservation_and_requeues_saved_repair(delivered, tmp_path, monkeypatch, pool_full):
    _, out, payload, _, root, _, publication = archive(delivered, tmp_path)
    key = payload["repo"] + "#123"
    (root / "queue").mkdir()
    entry = {"id": key, "repo": payload["repo"], "number": 123, "status": "running",
             "staged_at": (business_now() - dt.timedelta(hours=2)).isoformat(),
             "dispatch_job_id": payload["job_id"], "dispatch_started_at": business_now().isoformat(),
             "direct_start_reserved_at": business_now().isoformat(), "ai_verdict": {"usable": True}}
    queue = {key: entry}
    if pool_full:
        from scout_v2.console_models import write_models
        write_models(root, {"candidate_pool_size": "1"})
        queue["pypa/pipx#124"] = {"id": "pypa/pipx#124", "status": "staged", "staged_at": business_now().isoformat()}
    (root / "queue/candidates.json").write_text(json.dumps(queue))
    ledger = ClaimLedger(root / "state/scout_v2.sqlite3")
    assert ledger.try_claim("Asia/Shanghai", key)[0]
    assert ledger.reserve_publication("Asia/Shanghai", 5, payload["job_id"], payload["repo"],
                                      "xixi-box:fix/issue-123-abcdef0-12345678", "xixi-box/oss-scout")
    ledger.set_publication_run(payload["job_id"], "12345")
    monkeypatch.setattr(pipeline, "gh_read", lambda args:
        {"display_title": "repair " + payload["job_id"], "status": "completed", "conclusion": "failure"}
        if "/actions/runs/" in args[0] else [])
    result = json.loads((out / "result.json").read_text())
    result["publish"] = publication
    try:
        assert pipeline._reconcile_publication(ledger, "Asia/Shanghai", root=root,
            worker_result=result, job_id=payload["job_id"]) == {}
        saved = json.loads((root / "queue/candidates.json").read_text(encoding="utf-8"))[key]
        assert saved["status"] == ("queue_evicted" if pool_full else "staged")
        assert saved["repair_outcome"] == "verified_publication_retry"
        assert saved["verified_delivery_source"]["job_id"] == payload["job_id"]
        assert not saved.get("dispatch_job_id") and not saved.get("direct_start_reserved_at")
        assert ledger.dispatch_capacity("Asia/Shanghai", 5)["active"] == 0
        assert ledger.quota_used(business_now().date().isoformat()) == 0
        assert ledger.try_claim("Asia/Shanghai", key, retry_failed=True)[0]
        if pool_full:
            current = json.loads((root / "queue/candidates.json").read_text(encoding="utf-8"))
            assert sum(item["status"] == "staged" for item in current.values()) == 1
            records = [json.loads(line) for line in (root / "state/worklog.jsonl").read_text(encoding="utf-8").splitlines()]
            assert any(row["action"] == "pipeline_queue_evicted" and row["target"] == key for row in records)
    finally:
        ledger.close()


def test_issue_snapshot_transport_failure_uses_the_same_completed_run_retry_policy():
    assert pipeline._retryable_codex_communication([
        "controller communication unavailable: issue snapshot download timed out"])
    assert not pipeline._retryable_codex_communication(["missing target assertion"])


@pytest.mark.parametrize("pool_full", [False, True])
def test_explicit_old_rate_limit_restore_defaults_to_dry_run_and_cannot_touch_remote_writer(delivered, tmp_path, monkeypatch, pool_full):
    from scripts import retry_verified_publication as script
    _, _, payload, _, root, _, _ = archive(delivered, tmp_path)
    key = payload["repo"] + "#123"
    (root / "queue").mkdir()
    entry = {"id": key, "repo": payload["repo"], "number": 123, "status": "aborted_pipeline",
             "staged_at": (business_now() - dt.timedelta(hours=2)).isoformat(),
             "repair_job_id": payload["job_id"]}
    queue = root / "queue/candidates.json"
    initial = {key: entry}
    if pool_full:
        from scout_v2.console_models import write_models
        write_models(root, {"candidate_pool_size": "1"})
        initial["pypa/pipx#124"] = {"id": "pypa/pipx#124", "status": "staged", "staged_at": business_now().isoformat()}
    queue.write_text(json.dumps(initial))
    ledger = ClaimLedger(root / "state/scout_v2.sqlite3")
    ledger.dispatch_capacity("Asia/Shanghai", 5)
    ledger.close()
    now = business_now()
    monkeypatch.setattr(script, "gh_read", lambda args:
        {"display_title": "repair " + payload["job_id"], "status": "completed",
         "path": ".github/workflows/oss-scout-repair.yml", "updated_at": (now - dt.timedelta(hours=2)).isoformat()}
        if "/actions/runs/" in args[0] else [])
    snapshot = queue.read_bytes()
    dry = script.authorize_retry(root, payload["job_id"])
    assert dry["applied"] is False and queue.read_bytes() == snapshot
    assert not (root / "state/worklog.jsonl").exists()
    applied = script.authorize_retry(root, payload["job_id"], apply=True)
    assert applied["applied"] is True
    saved = json.loads(queue.read_text(encoding="utf-8"))[key]
    assert saved["status"] == ("queue_evicted" if pool_full else "staged")
    assert saved["rescreen_pending"] is True
    assert applied["candidate_status"] == saved["status"] and applied["evicted"] == ([key] if pool_full else [])
    assert saved["retry_authorized_job_id"] == payload["job_id"]
    assert saved["verified_delivery_source"]["job_id"] == payload["job_id"]
    records = [json.loads(line) for line in (root / "state/worklog.jsonl").read_text(encoding="utf-8").splitlines()]
    assert records[0]["action"] == "authorize_verified_publication_retry"
    if pool_full:
        assert records[-1]["action"] == "pipeline_queue_evicted"
        assert sum(item["status"] == "staged" for item in json.loads(queue.read_text(encoding="utf-8")).values()) == 1
    with pytest.raises(ValueError, match="no longer matches"):
        script.authorize_retry(root, payload["job_id"], apply=True)


def test_download_failure_still_returns_structured_diagnostics_and_prevents_worker_start(tmp_path, monkeypatch, capsys):
    job = tmp_path / "job.json"
    job.write_text(json.dumps({"job_id": "job-restored", "base_sha": "a" * 40}))
    output = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(sys, "argv", ["recovery", "--job", str(job), "--destination", str(tmp_path / "input")])
    def unavailable(*_):
        raise subprocess.TimeoutExpired("private-credential-in-command", 120)
    monkeypatch.setattr(recovery, "download_source", unavailable)
    recovery.main()
    result = json.loads((tmp_path / "artifacts/result.json").read_text())
    assert result["outcome"] == "failed" and result["job_id"] == "job-restored"
    assert "TimeoutExpired" in result["unverified_items"][0]
    assert "private" not in json.dumps(result)
    assert output.read_text() == "ready=false\n"
    assert "##OSSCOUT_RESULT##" in capsys.readouterr().out


def test_no_retained_source_is_a_noop_with_ready_output(tmp_path, monkeypatch):
    job = tmp_path / "job.json"
    job.write_text(json.dumps({"job_id": "job-new"}))
    output = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(sys, "argv", ["recovery", "--job", str(job), "--destination", str(tmp_path / "input")])
    recovery.main()
    assert output.read_text() == "ready=true\n" and not (tmp_path / "input").exists()
