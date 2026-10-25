"""P3 测试：Actions 后端（mock gh）+ 收集校验 + broad_scan 拒绝路由。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scout_v2.adapters import actions_backend as ab  # noqa: E402
from scout_v2.schemas import Job  # noqa: E402


def job(mode="repair", sha="b" * 40) -> Job:
    return Job(mode=mode, repo="a/b", base_sha=sha,
               idempotency_key=f"repair:a/b:{sha}:fp",
               execution_scope="project_contribution")


class FakeGH:
    """按调用序返回脚本化响应。"""
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[list[str]] = []

    def __call__(self, args):
        self.calls.append(args)
        return self.responses.pop(0) if self.responses else (1, "script exhausted")


class TestEligibility:
    def test_broad_scan_always_ineligible(self):
        b = ab.ActionsBackend(enabled=True, repo="me/t", workflow_exists=True)
        j = job("repair"); j2 = j.to_dict(); j2["execution_scope"] = "broad_scan"
        assert not b.eligible(j2)

    def test_scan_reproduce_eligible_on_contribution(self):
        """能力对称裁定 2026-10-25：scan/reproduce 在 project_contribution 范围内
        与 repair/review/ci 同权；broad_scan 仍恒拒。"""
        b = ab.ActionsBackend(enabled=True, repo="me/t", workflow_exists=True)
        assert b.eligible(job("scan").to_dict())
        assert b.eligible(job("reproduce").to_dict())

    def test_disabled_or_no_workflow_ineligible(self):
        j = job().to_dict()
        assert not ab.ActionsBackend(enabled=False, repo="me/t", workflow_exists=True).eligible(j)
        assert not ab.ActionsBackend(enabled=True, repo="me/t", workflow_exists=False).eligible(j)

    def test_contribution_ok(self):
        b = ab.ActionsBackend(enabled=True, repo="me/t", workflow_exists=True)
        assert b.eligible(job().to_dict())


class TestSubmit:
    def test_dispatch_and_find_run(self):
        gh = FakeGH([(0, ""), (0, json.dumps({"workflow_runs": [{"id": 12345, "event": "workflow_dispatch"}]}))])
        b = ab.ActionsBackend(enabled=True, repo="me/t", workflow_exists=True, gh_runner=gh)
        run_id = b.submit(job().to_dict())
        assert run_id == "12345"
        assert gh.calls[0][2].endswith("/dispatches")

    def test_dispatch_failure_raises(self):
        gh = FakeGH([(1, "404 no workflow")])
        b = ab.ActionsBackend(enabled=True, repo="me/t", workflow_exists=True, gh_runner=gh)
        with pytest.raises(RuntimeError, match="dispatch failed"):
            b.submit(job().to_dict())

    def test_run_not_found_raises(self):
        gh = FakeGH([(0, "")] + [(0, json.dumps({"workflow_runs": []}))] * 8)
        b = ab.ActionsBackend(enabled=True, repo="me/t", workflow_exists=True, gh_runner=gh)
        with pytest.raises(RuntimeError, match="not found"):
            b.submit(job().to_dict())


class TestPollCollect:
    def test_poll_states(self):
        gh = FakeGH([
            (0, json.dumps({"status": "in_progress"})),
            (0, json.dumps({"status": "completed", "conclusion": "success"})),
        ])
        b = ab.ActionsBackend(enabled=True, repo="me/t", gh_runner=gh)
        assert b.poll("1")["state"] == "running"
        assert b.poll("1")["state"] == "succeeded"

    def test_collect_reads_artifact(self, tmp_path):
        # 先准备本地 artifact 目录，模拟 gh run download 的产物
        run_id = "777"
        dest = tmp_path / f"run-{run_id}"
        (dest / "result.json").parent.mkdir(parents=True)
        (dest / "result.json").write_text(json.dumps(
            {"job_id": "job-1", "outcome": "succeeded", "base_sha": "b" * 40}),
            encoding="utf-8")

        gh = FakeGH([(0, json.dumps({"artifacts": [{"id": 9}]}))])
        dl = FakeGH([(0, "")])
        b = ab.ActionsBackend(enabled=True, repo="me/t", gh_runner=gh,
                              download_runner=dl, artifact_dir=tmp_path)
        collected = b.collect(run_id)
        assert collected["job_id"] == "job-1"
        # 校验器：一致 → 无错
        j = job(sha="b" * 40); j.job_id = "job-1"
        assert ab.verify_collected(collected, j) == []
        # 校验器：SHA 不一致 → 报错
        j2 = job(sha="c" * 40); j2.job_id = "job-1"
        assert ab.verify_collected(collected, j2)

    def test_collect_no_artifacts_falls_back_to_log_envelope(self, tmp_path):
        gh = FakeGH([(0, json.dumps({"artifacts": []}))])
        log = "step1 ok\n##OSSCOUT_RESULT##" + json.dumps(
            {"result": {"job_id": "job-1", "outcome": "succeeded"}})
        b = ab.ActionsBackend(enabled=True, repo="me/t", gh_runner=gh, artifact_dir=tmp_path)
        b._logs_text = lambda rid: log
        out = b.collect("1")
        assert out["job_id"] == "job-1" and out["outcome"] == "succeeded"

    def test_collect_no_artifacts_no_envelope(self, tmp_path):
        gh = FakeGH([(0, json.dumps({"artifacts": []}))])
        b = ab.ActionsBackend(enabled=True, repo="me/t", gh_runner=gh, artifact_dir=tmp_path)
        b._logs_text = lambda rid: "plain logs, no marker"
        out = b.collect("1")
        assert out["outcome"] == "inconclusive"
        assert any("envelope" in u for u in out["unverified_items"])

    def test_collect_logs_unavailable(self, tmp_path):
        gh = FakeGH([(0, json.dumps({"artifacts": []}))])
        b = ab.ActionsBackend(enabled=True, repo="me/t", gh_runner=gh, artifact_dir=tmp_path)
        b._logs_text = lambda rid: None
        out = b.collect("1")
        assert out["outcome"] == "inconclusive"
        assert any("logs unavailable" in u for u in out["unverified_items"])

    def test_cancel(self):
        gh = FakeGH([(0, "")])
        b = ab.ActionsBackend(enabled=True, repo="me/t", gh_runner=gh)
        b.cancel("42")
        assert gh.calls[0][0] == "-X" and gh.calls[0][1] == "DELETE" \
            and "runs/42" in gh.calls[0][2]
