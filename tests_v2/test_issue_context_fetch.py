import io
import json
import urllib.error

import pytest

from scout_v2.workers.issue_context import load_issue_context


@pytest.fixture
def context_job(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_URL", "https://controller.example")
    monkeypatch.setenv("INGEST_TOKEN", "do-not-log-this")
    job_id = "job-12345678-1234-1234-1234-123456789abc"
    job = {"job_id": job_id, "base_sha": "a" * 40, "context": f"oss-scout-snapshot:{job_id}"}
    path = tmp_path / "job.json"
    path.write_text(json.dumps(job))
    return path, tmp_path / "artifacts"


def test_snapshot_retries_timeout_then_preserves_complete_evidence(context_job):
    attempts = []
    delays = []
    snapshot = {"issue": {"body": "full report"}, "comments": [{"body": "完整讨论"}]}

    def opener(request, timeout):
        attempts.append(request)
        if len(attempts) < 3:
            raise TimeoutError("do-not-log-this")
        return io.BytesIO(json.dumps(snapshot).encode())

    path, out = context_job
    assert load_issue_context(path, out, opener=opener, sleep=delays.append)
    assert len(attempts) == 3
    assert delays == [1, 2]
    assert "完整讨论" in json.loads(path.read_text(encoding="utf-8"))["context"]
    assert not (out / "result.json").exists()


@pytest.mark.parametrize("status,expected_attempts,retryable", [(503, 4, True), (429, 4, True), (403, 1, False)])
def test_snapshot_failure_is_structured_without_leaking_credentials(context_job, capsys,
                                                                   status, expected_attempts, retryable):
    attempts = []
    path, out = context_job

    def opener(request, timeout):
        attempts.append(1)
        raise urllib.error.HTTPError(request.full_url, status, "do-not-log-this", {}, None)

    assert not load_issue_context(path, out, opener=opener, sleep=lambda _: None)
    result = json.loads((out / "result.json").read_text(encoding="utf-8"))
    diagnostic = (out / "issue-context-fetch.json").read_text(encoding="utf-8")
    assert result["outcome"] == "failed"
    assert len(attempts) == expected_attempts
    assert ("controller communication unavailable" in result["unverified_items"][0]) is retryable
    assert "do-not-log-this" not in diagnostic + json.dumps(result) + capsys.readouterr().out
    assert json.loads(path.read_text(encoding="utf-8"))["context"].startswith("oss-scout-snapshot:")
