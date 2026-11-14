import hashlib
import json
import subprocess

import pytest

from scout_v2.workers import delivery_review as review
from scout_v2.workers import opencode_client
from test_p5_repair import GOOD_PATCH, make_repo


@pytest.fixture(params=["opencode", "codex"])
def delivery(request, tmp_path):
    repo, sha = make_repo(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    (out / "change.patch").write_bytes(GOOD_PATCH.encode())
    job = {"job_id": "job-review", "base_sha": sha, "repo": "pypa/pipx",
           "repair_agent": request.param, "model": "configured-model#medium"}
    report = {"job_id": job["job_id"], "base_sha": sha, "base_returncode": 1, "patched_returncode": 0,
              "failure_kind": None, "delivery_patch_sha256": hashlib.sha256(GOOD_PATCH.encode()).hexdigest()}
    (out / "test-report.json").write_text(json.dumps(report))
    evidence = {"ok": True, "snapshot": {"issue": {"state": "open"}, "comments": [], "timeline": []}}
    return job, repo, out, evidence


class Reviewer:
    def __init__(self, verdict="approve", mutate=False, **kwargs):
        self.verdict = verdict
        self.mutate = mutate

    def run(self, model, prompt):
        inputs = json.loads((self.workdir / "OSSCOUT_REVIEW_INPUT.json").read_text())
        assert inputs["verified_patch"] == GOOD_PATCH
        assert "Chinese and English" in prompt
        assert "return a / b" in (self.workdir / "calc.py").read_text()
        if self.mutate:
            (self.workdir / "calc.py").write_text("modified by reviewer")
        return True, json.dumps({"verdict": self.verdict, "findings": [] if self.verdict == "approve"
                                 else [{"file": "calc.py", "reason": "behavioral regression not covered"}]})


def test_both_tools_bind_review_to_patch_and_latest_intent(delivery):
    job, repo, out, evidence = delivery
    result = review.review_delivery(job, repo, out, evidence, client=Reviewer())
    assert result["verdict"] == "approve"
    assert review.review_approved(job, out, evidence)
    changed = {**evidence, "snapshot": {**evidence["snapshot"], "comments": [{"body": "这个方向我们不接受"}]}}
    assert not review.review_approved(job, out, changed)
    (out / "change.patch").write_text(GOOD_PATCH + "\n")
    assert not review.review_approved(job, out, evidence)


def test_read_only_review_rejects_mutation_and_substantive_findings(delivery):
    job, repo, out, evidence = delivery
    result = review.review_delivery(job, repo, out, evidence, client=Reviewer(mutate=True))
    assert result["verdict"] == "blocked" and "changed" in result["error"]
    assert not review.review_approved(job, out, evidence)
    result = review.review_delivery(job, repo, out, evidence, client=Reviewer("changes_requested"))
    assert result["verdict"] == "changes_requested"
    assert not review.review_approved(job, out, evidence)


def test_production_review_factory_enforces_read_only(delivery, monkeypatch):
    job, repo, out, evidence = delivery
    kwargs_seen = []

    def factory(**kwargs):
        kwargs_seen.append(kwargs)
        client = Reviewer()
        client.workdir = kwargs["workdir"]
        return client

    from scout_v2.workers import codex_client
    monkeypatch.setattr(codex_client, "CodexClient", factory)
    monkeypatch.setattr(opencode_client, "OpenCodeClient", factory)
    result = review.review_delivery(job, repo, out, evidence)
    assert result["verdict"] == "approve"
    assert kwargs_seen[0]["read_only"] is True and kwargs_seen[0]["timeout_seconds"] == 300


def test_opencode_reviewer_has_no_publish_secrets_and_no_retries(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "write-secret")
    monkeypatch.setenv("CF_INGEST_TOKEN", "broker-secret")
    monkeypatch.setenv("OPENCODE_API_KEY", "model-secret")
    calls = []

    def run(args, **kwargs):
        calls.append(kwargs)
        env = kwargs["env"]
        assert "GH_TOKEN" not in env and "CF_INGEST_TOKEN" not in env
        assert env["OPENCODE_API_KEY"] == "model-secret"
        permissions = json.loads(env["OPENCODE_CONFIG_CONTENT"])["permission"]
        assert all(permissions[key] == "deny" for key in ("edit", "bash", "webfetch", "external_directory"))
        return subprocess.CompletedProcess(args, 1, "", "connection reset")

    monkeypatch.setattr(opencode_client.subprocess, "run", run)
    assert not opencode_client.OpenCodeClient(workdir=tmp_path, read_only=True).run("model", "review")[0]
    assert len(calls) == 1
