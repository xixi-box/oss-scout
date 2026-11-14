import hashlib
import json
from types import SimpleNamespace

import pytest

from scout_v2.workers import publish_remote, delivery_review
from test_delivery_quality import delivered


def test_verified_but_unpublished_delivery_is_retained(delivered, monkeypatch):
    repo, out, payload, _ = delivered
    evidence = {"ok": True, "snapshot": {"issue": {"state": "open"}, "comments": []}}
    class Approve:
        def run(self, *_):
            return True, '{"verdict":"approve","findings":[]}'
    assert delivery_review.review_delivery(payload, repo, out, evidence, client=Approve())["verdict"] == "approve"
    monkeypatch.setattr(publish_remote, "refresh_publication_evidence", lambda *_: evidence)
    monkeypatch.setattr(publish_remote, "publish", lambda *_: {"ok": False, "error": "CreatePullRequest permission denied"})
    result = publish_remote.publish_from_env(payload, repo, out)
    assert result["failure_kind"] == "publication_permission_denied"
    manifest = json.loads((out / "verified-delivery/manifest.json").read_text())
    assert manifest["patch_sha256"] == hashlib.sha256((out / "change.patch").read_bytes()).hexdigest()
    assert (out / "verified-delivery/result.json").exists()


@pytest.mark.parametrize("block", ["missing_review", "changed_discussion", "cap_full"])
def test_publication_never_bypasses_fresh_evidence_or_review(delivered, monkeypatch, block):
    repo, out, payload, _ = delivered
    original = {"ok": True, "snapshot": {"issue": {"state": "open"}, "comments": []}}
    if block != "missing_review":
        class Approve:
            def run(self, *_):
                return True, '{"verdict":"approve","findings":[]}'
        delivery_review.review_delivery(payload, repo, out, original, client=Approve())
    current = original
    if block == "changed_discussion":
        current = {"ok": True, "snapshot": {"issue": {"state": "open"}, "comments": [{"body": "请不要这样修改"}]}}
    if block == "cap_full":
        current = {"ok": False, "error": "仓库同时开放PR已达5个", "coarse_reason": "仓库同时开放PR已达5个",
                   "failure_kind": "publication_open_pr_cap"}
    monkeypatch.setattr(publish_remote, "refresh_publication_evidence", lambda *_: current)
    monkeypatch.setattr(publish_remote, "publish", lambda *_: pytest.fail("must not attempt a GitHub write"))
    result = publish_remote.publish_from_env(payload, repo, out)
    assert result["ok"] is False
    assert result["failure_kind"] == ("publication_open_pr_cap" if block == "cap_full" else "code_review_required")
    assert (out / "verified-delivery/change.patch").exists()


def test_repository_pr_cap_is_a_coarse_gate(tmp_path, monkeypatch):
    from dashboard import data
    repo = "NousResearch/hermes-agent"
    engine = SimpleNamespace(repos_cfg={"repositories": {repo: {}}}, repo_entry=lambda *_: {})
    item = {"repo": repo, "number": 1, "state": "open", "assignees": [], "labels": []}
    monkeypatch.setattr(data, "_publication_capacity", lambda *_: {"ok": False, "failure_kind": "publication_open_pr_cap",
                                                                 "coarse_reason": "仓库同时开放PR已达5个"})
    assert data.coarse_issue_blockers(engine, item, lane="open", root=tmp_path) == ["仓库同时开放PR已达5个"]


def test_workflow_keeps_review_separate_from_publish_credentials():
    from pathlib import Path
    import yaml
    workflow = yaml.safe_load((Path(__file__).parents[1] / ".github/workflows/oss-scout-repair.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["repair"]["steps"]
    review = next(step for step in steps if step.get("name") == "Read-only delivery review")
    assert "GH_TOKEN" not in review.get("env", {}) and "GITHUB_TOKEN" not in review.get("env", {})
    retention = next(step for step in steps if step.get("name") == "Upload verified delivery")
    assert retention["with"]["retention-days"] == 30
    assert steps.index(review) < next(i for i, step in enumerate(steps) if step.get("name", "").startswith("Publish ("))


def test_git_push_uses_same_preferred_credential_as_gh(tmp_path, monkeypatch):
    import subprocess
    patch = tmp_path / "change.patch"
    patch.write_text("--- a/x\n+++ b/x\n")
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "https://github.com/up/repo/pull/1", "")
    monkeypatch.setenv("GH_TOKEN", "preferred-personal-token")
    monkeypatch.setenv("GITHUB_TOKEN", "limited-built-in-token")
    monkeypatch.setattr(publish_remote, "_run", run)
    monkeypatch.setattr(publish_remote.subprocess, "run", run)
    result = publish_remote.publish(tmp_path, patch, {"push_repo": "me/fork", "upstream": "up/repo",
                                                     "branch": "fix/1", "title": "fix: bug"})
    assert result["ok"]
    push = next(args for args in calls if args[:2] == ["git", "push"])
    assert any("preferred-personal-token@" in arg for arg in push)
    assert not any("limited-built-in-token" in arg for arg in push)
