"""统一执行器入口（计划 §5）：

    python -m scout_v2.workers.core --job /workspace/job.json --out /workspace/artifacts

P1 裁定：本入口在本地/云端执行**同一份**模式逻辑；GitHub 写能力不存在于本文件，
任何评论/Fork/Push/PR 由 VPS 可信控制器完成。P1 的 scan/reproduce/repair 在本地
后端是骨架（产出明确 inconclusive + unverified_items），P2/P3 接入真实执行。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from ..schemas import SCHEMA_VERSION, Job, Result
from .quality_gate import check_patch_path


def execute(job: Job, out_dir: Path, backend: str = "local") -> Result:
    """模式分派。所有模式共享约定：worker 无 GitHub 写令牌；结果只写 out_dir。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    errs = job.validate()
    if errs:
        return Result(job_id=job.job_id, backend=backend, outcome="blocked",
                      base_sha=job.base_sha, unverified_items=errs)
    if job.mode == "scan":
        return _run_scan(job, out_dir, backend)
    if job.mode == "reproduce":
        return _run_reproduce(job, out_dir, backend)
    if job.mode in {"repair", "review", "ci"}:
        return _run_repair_like(job, out_dir, backend)
    return Result(job_id=job.job_id, backend=backend, outcome="blocked",
                  base_sha=job.base_sha, unverified_items=[f"unsupported mode {job.mode}"])


def _run_scan(job: Job, out_dir: Path, backend: str) -> Result:
    # P2 接入 Semgrep/语言原生 lint。P1：骨架，产出 inconclusive，不做任何仓库执行。
    return Result(job_id=job.job_id, backend=backend, outcome="inconclusive",
                  base_sha=job.base_sha,
                  unverified_items=["scan engine not implemented (P2); skeleton only"])


def _run_reproduce(job: Job, out_dir: Path, backend: str) -> Result:
    # P2 接入隔离测试环境（无密钥、无网络默认）。P1：骨架。
    return Result(job_id=job.job_id, backend=backend, outcome="inconclusive",
                  base_sha=job.base_sha,
                  unverified_items=["reproduce harness not implemented (P2); skeleton only"])


def _run_repair_like(job: Job, out_dir: Path, backend: str) -> Result:
    # P3 接入 OpenCode + 项目工作流。P1：骨架。
    return Result(job_id=job.job_id, backend=backend, outcome="inconclusive",
                  base_sha=job.base_sha,
                  unverified_items=[f"{job.mode} engine not implemented (P3); skeleton only"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True, help="path to job.json")
    ap.add_argument("--out", required=True, help="artifacts output directory")
    ap.add_argument("--backend", default="local", choices=["modal", "github_actions", "local"])
    args = ap.parse_args(argv)

    job_data = json.loads(Path(args.job).read_text(encoding="utf-8"))
    if job_data.get("allow_github_write"):
        print("[core] refuse: allow_github_write must be false (workers have no write token)",
              file=sys.stderr)
        return 2
    job = Job.from_dict(job_data)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    result = execute(job, out_dir, backend=args.backend)
    # 出口前最后一道：结果内路径不得越界
    for p in (result.patch_path, result.test_report_path, result.logs_path):
        if check_patch_path(p):
            print(f"[core] refuse: unsafe artifact path {p!r}", file=sys.stderr)
            return 2
    payload = json.dumps(result.to_dict(), ensure_ascii=False, indent=1)
    (out_dir / "result.json").write_text(payload, encoding="utf-8")
    print(f"[core] wrote {out_dir / 'result.json'} outcome={result.outcome}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
