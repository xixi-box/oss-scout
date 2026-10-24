"""质量与安全闸（计划 §10/§16）：结果导入前的确定性校验，与后端无关。"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ..schemas import Job, Result

# Result 内路径必须是安全的相对 artifact 路径
_UNSAFE_PATH = re.compile(r"^([A-Za-z]:|\\\\|/|\.\.)")


@dataclass
class VerifyReport:
    ok: bool
    errors: list[str]


def check_patch_path(patch_path: str | None) -> list[str]:
    """补丁路径校验：拒绝绝对路径、盘符、..、反斜杠注入（计划 §10 路径 traversal）。"""
    errs = []
    if patch_path is None:
        return errs
    if _UNSAFE_PATH.search(patch_path) or "\\" in patch_path:
        errs.append(f"unsafe patch path: {patch_path!r}")
        return errs
    p = PurePosixPath(patch_path)
    if p.is_absolute() or ".." in p.parts:
        errs.append(f"unsafe patch path: {patch_path!r}")
    return errs


def verify_result(result: Result, job: Job, *,
                  expected_base_sha: str | None = None,
                  expected_fingerprint: str | None = None,
                  artifact_dir: Path | None = None,
                  patch_sha256: str | None = None) -> VerifyReport:
    """可信控制器导入 Result 前的校验（计划 §4.3/§16）。

    - base SHA 不一致 → 禁止导入（防错库错 commit 的补丁被 Push）。
    - job_id 必须对应。
    - patch 路径必须安全，且（给了 hash 时）内容校验一致。
    - 未经 reproduce_on_base 证实不得宣称 fixed。
    """
    errs: list[str] = []
    if result.job_id != job.job_id:
        errs.append(f"job_id mismatch: result={result.job_id!r} job={job.job_id!r}")
    if expected_base_sha and result.base_sha and result.base_sha != expected_base_sha:
        errs.append(f"base_sha mismatch: result={result.base_sha} expected={expected_base_sha}")
    if expected_fingerprint and expected_fingerprint not in job.idempotency_key:
        errs.append("candidate fingerprint not in idempotency key — wrong lineage")
    errs.extend(check_patch_path(result.patch_path))
    if patch_sha256 is not None and result.patch_path and artifact_dir is not None:
        f = artifact_dir / result.patch_path
        if not f.is_file():
            errs.append(f"patch artifact missing: {result.patch_path}")
        else:
            actual = hashlib.sha256(f.read_bytes()).hexdigest()
            if actual != patch_sha256:
                errs.append("patch artifact hash mismatch")
    if result.outcome == "succeeded":
        if result.fixed_on_patch and not result.regression_tests_passed:
            errs.append("cannot claim fixed without regression_tests_passed")
        if result.fixed_on_patch and not result.reproduced_on_base:
            errs.append("cannot claim fixed without reproduced_on_base (original must fail first)")
    return VerifyReport(ok=not errs, errors=errs)
