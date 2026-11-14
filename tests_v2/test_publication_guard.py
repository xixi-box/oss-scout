import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scout_v2.git_host import RecheckResult
from scout_v2.workers import publication_guard as guard


def payload():
    return {"job_id": "job-example", "repo": "acme/project", "base_sha": "a" * 40,
            "repair_agent": "codex", "context": "must not be persisted",
            "publish": {"upstream": "acme/project", "push_repo": "xixi-box/project",
                        "issue_number": 7, "branch": "fix/issue-7-example"}}


def latest(monkeypatch, **overrides):
    issue = {"number": 7, "state": "open", "assignees": [], "labels": [], "body": "reported bug"}
    comments = [{"body": "完整中文讨论", "user": {"login": "maintainer"}, "author_association": "MEMBER"}]
    result = RecheckResult(complete=True, issue_open=True, unassigned=True,
                           screening_source=(issue, comments, []))
    for key, value in overrides.items():
        setattr(result, key, value)
    monkeypatch.setattr(guard, "recheck_candidate", lambda *a, **kw: result)
    monkeypatch.setattr(guard, "gh_read", lambda args: {"login": "xixi-box"} if args == ["user"] else {
        "full_name": "acme/project", "archived": False, "disabled": False,
        "permissions": {"pull": True, "push": False}})
    return result


def test_refresh_preserves_full_discussion_without_rejecting_external_read_access(monkeypatch):
    result = latest(monkeypatch)
    job = payload()
    evidence = guard.refresh_publication_evidence(job, job["publish"])
    assert evidence["ok"]
    assert evidence["snapshot"]["comments"] == result.screening_source[1]
    assert evidence["diagnostics"]["pull_request_creation"] == "not_probed"


@pytest.mark.parametrize("change", ["closed", "duplicate", "assigned", "claimed", "competition"])
def test_publish_time_changes_are_blocked(monkeypatch, change):
    result = latest(monkeypatch)
    issue = result.screening_source[0]
    if change == "closed":
        issue["state"] = "closed"
    elif change == "duplicate":
        result.is_duplicate = True
    elif change == "assigned":
        issue["assignees"] = [{"login": "someone-else"}]
    elif change == "claimed":
        result.has_claim_signal = True
    elif change == "competition":
        result.open_pr_hits = ["acme/project#8: Fixes #7"]
    job = payload()
    evidence = guard.refresh_publication_evidence(job, job["publish"])
    assert not evidence["ok"]
    assert evidence["coarse_reason"]
    assert evidence["snapshot"]["comments"]


@pytest.mark.parametrize("overrides", [
    {"complete": False}, {"errors": ["pr search failed (non-fatal)"]}, {"screening_source": None},
])
def test_incomplete_refresh_never_means_no_competition(monkeypatch, overrides):
    latest(monkeypatch, **overrides)
    job = payload()
    evidence = guard.refresh_publication_evidence(job, job["publish"])
    assert not evidence["ok"] and evidence["failure_kind"] == "publication_evidence_unavailable"


def test_assignment_requirement_and_account_mismatch_are_explicit(monkeypatch):
    result = latest(monkeypatch)
    job = payload()
    job["publish"]["assignment_rule"] = {"requires_assignment": True}
    assert "指派" in guard.refresh_publication_evidence(job, job["publish"])["coarse_reason"]
    result.screening_source[0]["assignees"] = [{"login": "xixi-box"}]
    assert guard.refresh_publication_evidence(job, job["publish"])["ok"]
    job["publish"]["push_repo"] = "another-user/project"
    assert "account" in guard.refresh_publication_evidence(job, job["publish"])["error"]


@pytest.mark.parametrize("metadata,kind", [
    ({"full_name": "acme/project", "archived": True, "disabled": False}, "publication_preflight_blocked"),
    ({"full_name": "acme/project", "archived": False, "disabled": True}, "publication_preflight_blocked"),
    ({"message": "Not Found"}, "publication_evidence_unavailable"),
])
def test_repository_unavailability_and_missing_metadata_are_blocked(monkeypatch, metadata, kind):
    latest(monkeypatch)
    monkeypatch.setattr(guard, "gh_read", lambda args: {"login": "xixi-box"} if args == ["user"] else metadata)
    job = payload()
    result = guard.refresh_publication_evidence(job, job["publish"])
    assert not result["ok"] and result["failure_kind"] == kind


PATCH = ("diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+fixed\n"
         "diff --git a/tests/test_app.py b/tests/test_app.py\n--- a/tests/test_app.py\n+++ b/tests/test_app.py\n"
         "@@ -1 +1 @@\n-old_test\n+regression_test\n")


def verified(tmp_path):
    job = payload()
    result = {"job_id": job["job_id"], "base_sha": job["base_sha"], "outcome": "succeeded",
              "reproduced_on_base": True, "fixed_on_patch": True, "regression_tests_passed": True}
    report = {"job_id": job["job_id"], "base_sha": job["base_sha"], "base_returncode": 1,
              "patched_returncode": 0, "failure_kind": None,
              "delivery_patch_sha256": hashlib.sha256(PATCH.encode()).hexdigest(),
              "delivery_files": ["app.py", "tests/test_app.py"],
              "regression_test_files": ["tests/test_app.py"], "test_cmd": ["pytest", "tests/test_app.py"],
              "root_cause": "wrong app behavior"}
    (tmp_path / "change.patch").write_bytes(PATCH.encode())
    for name, data in [("result.json", result), ("test-report.json", report)]:
        (tmp_path / name).write_text(json.dumps(data), encoding="utf-8")
    return job, report


def test_bundle_keeps_verified_patch_and_hashes_without_credentials(tmp_path, monkeypatch):
    job, _ = verified(tmp_path)
    monkeypatch.setenv("GH_TOKEN", "private-runtime-token")
    (tmp_path / "auth.json").write_text("credentials")
    (tmp_path / "codex-events.jsonl").write_text("events and credentials")
    (tmp_path / "code-review.json").write_text(json.dumps({"ok": True, "token": "hidden"}))
    publication = {"ok": False, "error": "permission refused private-runtime-token"}
    saved = guard.preserve_verified_bundle(job, tmp_path, publication)
    assert saved["ok"]
    target = Path(saved["path"])
    assert (target / "change.patch").read_bytes() == PATCH.encode()
    assert not (target / "auth.json").exists() and not (target / "codex-events.jsonl").exists()
    for name, digest in saved["manifest"]["files"].items():
        assert hashlib.sha256((target / name).read_bytes()).hexdigest() == digest
    all_text = "\n".join(p.read_text(encoding="utf-8") for p in target.iterdir())
    assert "private-runtime-token" not in all_text and "must not be persisted" not in all_text
    assert guard.preserve_verified_bundle(job, tmp_path, publication)["ok"]
    (target / "change.patch").write_text("tampered")
    assert not guard.preserve_verified_bundle(job, tmp_path, publication)["ok"]


@pytest.mark.parametrize("change", ["patch", "lineage", "manifest", "base_passes", "patched_fails", "unverified"])
def test_unverified_or_modified_deliveries_are_not_preserved(tmp_path, change):
    job, report = verified(tmp_path)
    if change == "patch":
        (tmp_path / "change.patch").write_text(PATCH + "modified")
    elif change == "lineage":
        report["base_sha"] = "b" * 40
    elif change == "manifest":
        report["delivery_files"] = ["app.py"]
    elif change == "base_passes":
        report["base_returncode"] = 0
    elif change == "patched_fails":
        report["patched_returncode"] = 1
    elif change == "unverified":
        result = json.loads((tmp_path / "result.json").read_text())
        result["outcome"] = "failed"
        (tmp_path / "result.json").write_text(json.dumps(result))
    (tmp_path / "test-report.json").write_text(json.dumps(report))
    assert not guard.preserve_verified_bundle(job, tmp_path, {"ok": False})["ok"]
    assert not (tmp_path / "verified-delivery").exists()


def test_artifact_symlink_escape_is_rejected(tmp_path):
    job, _ = verified(tmp_path)
    outside = tmp_path.parent / "outside-credentials"
    outside.write_text("must not be copied")
    path = tmp_path / "code-review.json"
    try:
        path.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation not available")
    assert not guard.preserve_verified_bundle(job, tmp_path, {})["ok"]
    assert not (tmp_path / "verified-delivery").exists()


def test_patch_with_runtime_secret_is_refused_without_changing_it(tmp_path, monkeypatch):
    job, report = verified(tmp_path)
    monkeypatch.setenv("OPENCODE_API_KEY", "private-runtime-key")
    patch = PATCH.replace("fixed", "private-runtime-key")
    (tmp_path / "change.patch").write_bytes(patch.encode())
    report["delivery_patch_sha256"] = hashlib.sha256(patch.encode()).hexdigest()
    (tmp_path / "test-report.json").write_text(json.dumps(report))
    assert not guard.preserve_verified_bundle(job, tmp_path, {})["ok"]
    assert (tmp_path / "change.patch").read_bytes() == patch.encode()


def test_target_directory_symlink_escape_is_rejected(tmp_path):
    job, _ = verified(tmp_path)
    outside = tmp_path.parent / "outside-bundle"
    outside.mkdir()
    try:
        (tmp_path / "verified-delivery").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation not available")
    assert not guard.preserve_verified_bundle(job, tmp_path, {})["ok"]
    assert not list(outside.iterdir())


def test_existing_manifest_symlink_is_never_read(tmp_path):
    job, _ = verified(tmp_path)
    saved = guard.preserve_verified_bundle(job, tmp_path, {})
    target = Path(saved["path"])
    outside = tmp_path.parent / "outside-manifest"
    outside.write_text(json.dumps(saved["manifest"]))
    (target / "manifest.json").unlink()
    try:
        (target / "manifest.json").symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation not available")
    result = guard.preserve_verified_bundle(job, tmp_path, {})
    assert not result["ok"] and "unsafe verified delivery manifest" in result["error"]


def test_malformed_json_object_is_not_preserved(tmp_path):
    job, _ = verified(tmp_path)
    (tmp_path / "result.json").write_text("[]")
    result = guard.preserve_verified_bundle(job, tmp_path, {})
    assert not result["ok"] and "metadata is not an object" in result["error"]


@pytest.mark.parametrize("count,ok", [(0, True), (4, True), (5, False), (6, False)])
def test_confirmed_hermes_open_pr_cap_includes_drafts(monkeypatch, count, ok):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout=json.dumps([
            {"number": n + 1, "isDraft": bool(n % 2)} for n in range(count)]))

    monkeypatch.setattr(guard.subprocess, "run", run)
    result = guard.check_open_pr_capacity("NousResearch/hermes-agent", "xixi-box")
    assert result["ok"] is ok and result["count"] == count and result["limit"] == 5
    assert commands[0][commands[0].index("--limit") + 1] == "6"
    assert commands[0][commands[0].index("--author") + 1] == "xixi-box"
    assert "--draft" not in commands[0]
    if not ok:
        assert result["failure_kind"] == "publication_open_pr_cap"
        assert result["coarse_reason"] == "仓库同时开放PR已达5个"


def test_unknown_repository_does_not_guess_an_open_pr_cap(monkeypatch):
    monkeypatch.setattr(guard.subprocess, "run", lambda *a, **kw: pytest.fail("must not request a guessed cap"))
    assert guard.check_open_pr_capacity("acme/project", "xixi-box") == {"ok": True, "count": None, "limit": None}


@pytest.mark.parametrize("response", [
    SimpleNamespace(returncode=1, stdout="[]"),
    SimpleNamespace(returncode=0, stdout='{"message":"Not Found"}'),
    SimpleNamespace(returncode=0, stdout='[{"number":1},{"number":1}]'),
])
def test_open_pr_read_failure_is_not_an_empty_queue(monkeypatch, response):
    monkeypatch.setattr(guard.subprocess, "run", lambda *a, **kw: response)
    result = guard.check_open_pr_capacity("NousResearch/hermes-agent", "xixi-box")
    assert not result["ok"] and result["count"] is None
    assert result["failure_kind"] == "publication_evidence_unavailable"


def test_publication_refresh_uses_configured_cap(monkeypatch):
    latest(monkeypatch)
    monkeypatch.setattr(guard.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=0, stdout='[{"number":1,"isDraft":true},{"number":2,"isDraft":false}]'))
    job = payload()
    job["publish"]["max_open_prs"] = 2
    result = guard.refresh_publication_evidence(job, job["publish"])
    assert not result["ok"] and result["failure_kind"] == "publication_open_pr_cap"
    assert result["coarse_reason"] == "仓库同时开放PR已达2个"


def test_platform_cap_exempts_writers_but_explicit_cap_still_applies(monkeypatch):
    monkeypatch.setattr(guard.subprocess, "run", lambda *a, **kw: pytest.fail("writer is exempt"))
    assert guard.check_open_pr_capacity("NousResearch/hermes-agent", "maintainer", has_write=True)["ok"]
    monkeypatch.setattr(guard.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=0, stdout='[{"number":1},{"number":2}]'))
    assert not guard.check_open_pr_capacity("NousResearch/hermes-agent", "maintainer", 2, has_write=True)["ok"]


def structured_reports(tmp_path, report, *, secret=False):
    for stage in ("base", "patched"):
        raw = f'<testsuite><testcase name="case">{stage}</testcase></testsuite>\r\n'.encode()
        if secret:
            raw += b"<!-- runtime-private-key -->"
        name = f"{stage}-regression.junit.xml"
        (tmp_path / name).write_bytes(raw)
        report[f"{stage}_evidence"] = {"format": "junit", "artifact": name,
                                       "sha256": hashlib.sha256(raw).hexdigest()}
    (tmp_path / "test-report.json").write_text(json.dumps(report))


def test_structured_reports_keep_original_bytes_and_verifier_hashes(tmp_path):
    job, report = verified(tmp_path)
    structured_reports(tmp_path, report)
    saved = guard.preserve_verified_bundle(job, tmp_path, {})
    assert saved["ok"]
    for stage in ("base", "patched"):
        name = f"{stage}-regression.junit.xml"
        assert (Path(saved["path"]) / name).read_bytes() == (tmp_path / name).read_bytes()
        assert saved["manifest"]["files"][name] == report[f"{stage}_evidence"]["sha256"]


@pytest.mark.parametrize("change", ["secret", "tampered", "oversize"])
def test_structured_report_secrets_hash_changes_and_oversize_are_refused(tmp_path, monkeypatch, change):
    job, report = verified(tmp_path)
    monkeypatch.setenv("OPENCODE_API_KEY", "runtime-private-key")
    structured_reports(tmp_path, report, secret=change == "secret")
    if change == "tampered":
        (tmp_path / "patched-regression.junit.xml").write_bytes(b"modified")
    elif change == "oversize":
        (tmp_path / "patched-regression.junit.xml").write_bytes(b"x" * 5_000_001)
    assert not guard.preserve_verified_bundle(job, tmp_path, {})["ok"]
    assert not (tmp_path / "verified-delivery").exists()


def collected_result(tmp_path, *, flattened=False, with_xml=False):
    job, report = verified(tmp_path)
    job["source_run"] = "43596880882"
    job["runner_repo"] = "xixi-box/oss-scout"
    extra = {}
    if with_xml:
        structured_reports(tmp_path, report)
        for stage in ("base", "patched"):
            name = f"{stage}-regression.junit.xml"
            extra[name] = (tmp_path / name).read_bytes()
    result = json.loads((tmp_path / "result.json").read_bytes())
    collected = result if flattened else {"result": result}
    collected.update(patch=(tmp_path / "change.patch").read_text(),
                     test_report=(tmp_path / "test-report.json").read_text(),
                     publish={"ok": False, "error": "publication limit"},
                     raw={"artifact_files": extra, "credentials": "must not be retained"})
    return job, report, collected


@pytest.mark.parametrize("flattened", [False, True])
def test_controller_log_fallback_keeps_patch_with_explicit_reverification_requirement(tmp_path, flattened):
    job, report, collected = collected_result(tmp_path, flattened=flattened)
    # Structured evidence can arrive in the stdout envelope without XML files.
    report["base_evidence"] = {"format": "junit", "artifact": "base-regression.junit.xml", "sha256": "missing"}
    collected["test_report"] = json.dumps(report)
    saved = guard.retain_collected_delivery(tmp_path, job, collected)
    assert saved["ok"] and saved["raw_reports_missing"] and saved["requires_reverification"]
    target = Path(saved["path"])
    assert target == tmp_path / "artifacts/verified-deliveries/job-example"
    assert (target / "change.patch").read_bytes() == PATCH.encode()
    assert saved["manifest"]["source_run"] == "43596880882"
    assert saved["manifest"]["source_repository"] == "xixi-box/oss-scout"
    assert saved["manifest"]["requires_publication_preflight"] is True
    assert "credentials" not in (target / "manifest.json").read_text()
    assert "raw" not in json.loads((target / "result.json").read_text())


def test_controller_artifact_retention_keeps_original_structured_reports(tmp_path):
    job, _, collected = collected_result(tmp_path, with_xml=True)
    collected["raw"]["artifact_files"]["auth.json"] = "must not be copied"
    collected["raw"]["artifact_files"]["code-review.json"] = {"verdict": "approve"}
    saved = guard.retain_collected_delivery(tmp_path, job, collected)
    assert saved["ok"] and not saved["raw_reports_missing"] and not saved["requires_reverification"]
    for name, value in collected["raw"]["artifact_files"].items():
        path = Path(saved["path"]) / name
        if name == "auth.json":
            assert not path.exists()
        elif isinstance(value, bytes):
            assert path.read_bytes() == value
    assert guard.retain_collected_delivery(tmp_path, job, collected)["ok"]


def test_worker_bundle_still_requires_raw_xml_while_controller_can_archive_missing_reports(tmp_path):
    job, report, collected = collected_result(tmp_path)
    report["base_evidence"] = {"format": "junit", "artifact": "base-regression.junit.xml", "sha256": "missing"}
    (tmp_path / "test-report.json").write_text(json.dumps(report))
    collected["test_report"] = report
    assert not guard.preserve_verified_bundle(job, tmp_path, {})["ok"]
    assert guard.retain_collected_delivery(tmp_path, job, collected)["requires_reverification"]


@pytest.mark.parametrize("change", ["patch_hash", "failed_result", "lineage", "xml_hash", "source_run", "runner_repo", "job_path"])
def test_controller_retention_refuses_invalid_delivery_or_source(tmp_path, change):
    job, report, collected = collected_result(tmp_path, with_xml=True)
    if change == "patch_hash":
        collected["patch"] += "modified"
    elif change == "failed_result":
        collected["result"]["outcome"] = "failed"
    elif change == "lineage":
        report["base_sha"] = "b" * 40
    elif change == "xml_hash":
        collected["raw"]["artifact_files"]["base-regression.junit.xml"] += b"modified"
    elif change == "source_run":
        job["source_run"] = "https://invalid/run"
    elif change == "runner_repo":
        job["runner_repo"] = "../repo"
    elif change == "job_path":
        job["job_id"] = "job-../../escape"
        report["job_id"] = job["job_id"]
        collected["result"]["job_id"] = job["job_id"]
    collected["test_report"] = report
    assert not guard.retain_collected_delivery(tmp_path, job, collected)["ok"]
    assert not (tmp_path / "artifacts").exists()


def test_controller_retention_never_overwrites_a_different_patch_or_modified_archive(tmp_path):
    job, report, collected = collected_result(tmp_path)
    saved = guard.retain_collected_delivery(tmp_path, job, collected)
    target = Path(saved["path"])
    collected["patch"] = PATCH.replace("fixed", "different_fix")
    report["delivery_patch_sha256"] = hashlib.sha256(collected["patch"].encode()).hexdigest()
    collected["test_report"] = report
    assert not guard.retain_collected_delivery(tmp_path, job, collected)["ok"]
    assert (target / "change.patch").read_bytes() == PATCH.encode()
    collected["patch"] = PATCH
    report["delivery_patch_sha256"] = hashlib.sha256(PATCH.encode()).hexdigest()
    (target / "result.json").write_text("tampered")
    assert not guard.retain_collected_delivery(tmp_path, job, collected)["ok"]
    assert (target / "result.json").read_text() == "tampered"


@pytest.mark.parametrize("which", ["artifacts", "target"])
def test_controller_retention_refuses_symlink_escape(tmp_path, which):
    job, _, collected = collected_result(tmp_path)
    outside = tmp_path.parent / f"outside-controller-{which}"
    outside.mkdir()
    path = tmp_path / "artifacts"
    if which == "target":
        path = path / "verified-deliveries"
        path.mkdir(parents=True)
        path = path / job["job_id"]
    try:
        path.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    assert not guard.retain_collected_delivery(tmp_path, job, collected)["ok"]
    assert not list(outside.iterdir())


def test_worker_manifest_records_only_validated_actions_origin(tmp_path):
    job, _ = verified(tmp_path)
    job["source_run"] = 43596880882
    saved = guard.preserve_verified_bundle(job, tmp_path, {})
    assert saved["ok"] and saved["manifest"]["source_run"] == "43596880882"
    assert saved["manifest"]["source_repository"] == "xixi-box/oss-scout"


def test_controller_archive_preserves_original_base_and_recovers_with_minimal_job_metadata(tmp_path):
    job, report, collected = collected_result(tmp_path)
    saved = guard.retain_collected_delivery(tmp_path, job, collected)
    minimal = {key: job[key] for key in ("job_id", "repo", "base_sha", "source_run")}
    assert guard.retain_collected_delivery(tmp_path, minimal, collected)["ok"]
    job["base_sha"] = "b" * 40
    report["base_sha"] = job["base_sha"]
    collected["result"]["base_sha"] = job["base_sha"]
    collected["test_report"] = report
    assert not guard.retain_collected_delivery(tmp_path, job, collected)["ok"]
    archived = json.loads((Path(saved["path"]) / "test-report.json").read_text())
    assert archived["base_sha"] == "a" * 40


@pytest.mark.parametrize("slug", ["../repo", "./repo", "_owner/repo", ".owner/repo", "owner/..", "owner/.", "owner/../repo"])
def test_actions_origin_rejects_path_components_and_invalid_owner(tmp_path, slug):
    job, _ = verified(tmp_path)
    job["runner_repo"] = slug
    assert not guard.preserve_verified_bundle(job, tmp_path, {})["ok"]
    assert not (tmp_path / "verified-delivery").exists()


@pytest.mark.parametrize("field", ["raw_reports_missing", "requires_reverification", "requires_publication_preflight"])
def test_controller_does_not_trust_modified_archive_flags(tmp_path, field):
    job, _, collected = collected_result(tmp_path)
    saved = guard.retain_collected_delivery(tmp_path, job, collected)
    path = Path(saved["path"]) / "manifest.json"
    manifest = json.loads(path.read_bytes())
    manifest[field] = not manifest[field]
    path.write_text(json.dumps(manifest))
    assert not guard.retain_collected_delivery(tmp_path, job, collected)["ok"]


def test_controller_checks_existing_xml_against_verifier_hash_not_only_manifest(tmp_path):
    job, _, collected = collected_result(tmp_path, with_xml=True)
    saved = guard.retain_collected_delivery(tmp_path, job, collected)
    target = Path(saved["path"])
    name = "base-regression.junit.xml"
    (target / name).write_bytes(b"modified report")
    manifest = json.loads((target / "manifest.json").read_bytes())
    manifest["files"][name] = hashlib.sha256(b"modified report").hexdigest()
    (target / "manifest.json").write_text(json.dumps(manifest))
    assert not guard.retain_collected_delivery(tmp_path, job, collected)["ok"]
