"""安全测试（计划 §16：SHA 校验、路径 traversal、secrets 不入产物、base→patch 证据链）。"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scout_v2.schemas import Job, Result  # noqa: E402
from scout_v2.workers.quality_gate import (check_patch_path,  # noqa: E402
                                           verify_result)


def job(job_id="job-1", sha="a" * 40, mode="reproduce", key="verify:a/b:" + "a" * 40 + ":fp") -> Job:
    return Job(job_id=job_id, mode=mode, repo="a/b", base_sha=sha, idempotency_key=key)


class TestPatchPathSafety:
    @pytest.mark.parametrize("bad", [
        "../escape.patch", "/abs/path.patch", "C:\\win\\path.patch",
        "..\\..\\x.patch", "dir/../../out.patch", "\\\\server\\share\\x",
    ])
    def test_unsafe_paths_rejected(self, bad):
        assert check_patch_path(bad)

    def test_safe_path_ok(self):
        assert not check_patch_path("artifacts/job-1/change.patch")


class TestResultVerification:
    def test_base_sha_mismatch_blocks_import(self):
        r = Result(job_id="job-1", backend="modal", outcome="succeeded",
                   base_sha="b" * 40)
        rep = verify_result(r, job(sha="a" * 40), expected_base_sha="a" * 40)
        assert not rep.ok and any("base_sha" in e for e in rep.errors)

    def test_job_id_mismatch_blocks(self):
        r = Result(job_id="other", backend="modal", outcome="inconclusive")
        assert not verify_result(r, job()).ok

    def test_claim_fixed_requires_repro_and_tests(self):
        r = Result(job_id="job-1", backend="modal", outcome="succeeded",
                   base_sha="a" * 40, fixed_on_patch=True,
                   regression_tests_passed=False, reproduced_on_base=True)
        rep = verify_result(r, job())
        assert not rep.ok
        r2 = Result(job_id="job-1", backend="modal", outcome="succeeded",
                    base_sha="a" * 40, fixed_on_patch=True,
                    regression_tests_passed=True, reproduced_on_base=False)
        rep2 = verify_result(r2, job())
        assert not rep2.ok

    def test_full_evidence_chain_passes(self):
        r = Result(job_id="job-1", backend="modal", outcome="succeeded",
                   base_sha="a" * 40, reproduced_on_base=True,
                   fixed_on_patch=True, regression_tests_passed=True,
                   patch_path="artifacts/job-1/change.patch")
        assert verify_result(r, job()).ok

    def test_artifact_hash_mismatch_blocks(self, tmp_path):
        art = tmp_path / "artifacts" / "job-1"
        art.mkdir(parents=True)
        (art / "change.patch").write_text("patch content", encoding="utf-8")
        import hashlib
        wrong = hashlib.sha256(b"other").hexdigest()
        r = Result(job_id="job-1", backend="modal", outcome="succeeded",
                   base_sha="a" * 40, patch_path="artifacts/job-1/change.patch")
        rep = verify_result(r, job(), artifact_dir=tmp_path, patch_sha256=wrong)
        assert not rep.ok and any("hash" in e for e in rep.errors)


class TestCoreNoSecrets:
    def test_result_contains_no_token_like_strings(self, tmp_path):
        from scout_v2.workers.core import execute
        j = job()
        r = execute(j, tmp_path)
        text = json.dumps(r.to_dict())
        for marker in ("ghp_", "github_pat_", "sk-", "MODAL_TOKEN", "GITHUB_TOKEN"):
            assert marker not in text

    def test_refuses_write_flag_in_job_file(self, tmp_path):
        from scout_v2.workers.core import main
        jf = tmp_path / "job.json"
        jf.write_text(json.dumps({"job_id": "j", "mode": "scan",
                                  "idempotency_key": "k",
                                  "allow_github_write": True}), encoding="utf-8")
        assert main(["--job", str(jf), "--out", str(tmp_path / "o")]) == 2

    def test_job_validate_rejects_write(self):
        j = job()
        j.allow_github_write = True
        assert any("allow_github_write" in e for e in j.validate())


class TestEnvKeyIsolation:
    def test_worker_process_env_has_no_tokens(self, tmp_path):
        """计划 §10：只在同进程清环境变量不可靠——这里验证的是 worker 子进程契约：
        job JSON 不携带任何 token 字段，core 不读取也不转发密钥。"""
        from scout_v2.workers.core import execute
        j = job()
        r = execute(j, tmp_path)
        assert r.backend == "local"
        # 骨架结果不含任何环境变量内容
        assert not any("PATH" in item or "KEY" in item
                       for item in r.unverified_items)
