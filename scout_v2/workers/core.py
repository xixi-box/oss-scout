"""统一执行器入口（计划 §5）：

    python -m scout_v2.workers.core --job /workspace/job.json --out /workspace/artifacts
    python -m scout_v2.workers.core --job-b64 <b64> --out ... --emit-envelope   # Modal Sandbox 用

本入口在本地/云端执行**同一份**模式逻辑；GitHub 写能力不存在于本文件，
任何评论/Fork/Push/PR 由 VPS 可信控制器完成。

- scan：Semgrep 精选规则 → 可疑线索（inconclusive，非证据）。
- reproduce：隔离脱敏环境跑最小失败测试（base 失败 → reproduced_on_base）。
- repair/review/ci：P3 接入 OpenCode 与项目工作流，当前骨架。
- --emit-envelope：stdout 单行信封（Modal Sandbox 无持久卷，结果走 stdout）。
"""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from ..schemas import Job, Result
from .quality_gate import check_patch_path

RESULT_MARKER = "##OSSCOUT_RESULT##"   # 与 adapters.modal_backend 一致
LOG_TAIL = 4000


def ensure_repo(repo_dir: Path, repo: str, base_sha: str) -> Path:
    """Sandbox 内按需拉取固定 SHA（公共 https 只读，无 token）。已有则直接复用。"""
    if repo_dir.exists() and (repo_dir / ".git").exists():
        return repo_dir
    url = f"https://github.com/{repo}.git"
    subprocess.run(["git", "clone", "--filter=blob:none", url, str(repo_dir)],
                   check=True, capture_output=True, text=True, timeout=900)
    # Modal Sandbox 以 root 跑，镜像内 /workspace 属于 worker → dubious ownership；
    # 本地 docker（worker 用户）同理可能有属主差。对当前用户加豁免（仅本沙箱内生效）。
    subprocess.run(["git", "config", "--global", "--add", "safe.directory",
                    str(repo_dir)], capture_output=True, timeout=60)
    co = subprocess.run(["git", "-C", str(repo_dir), "checkout", "--detach", base_sha],
                        capture_output=True, text=True, timeout=120)
    if co.returncode != 0:
        # 部分 clone 可能没取到目标 commit：显式 fetch 该 SHA 再试
        fe = subprocess.run(["git", "-C", str(repo_dir), "fetch", "origin", base_sha],
                            capture_output=True, text=True, timeout=300)
        co2 = subprocess.run(["git", "-C", str(repo_dir), "checkout", "--detach",
                              base_sha if fe.returncode == 0 else "FETCH_HEAD"],
                             capture_output=True, text=True, timeout=120)
        if co2.returncode != 0:
            raise RuntimeError(
                f"checkout {base_sha[:12]} failed: "
                f"{(co.stderr or co2.stderr or fe.stderr).strip()[-300:]}")
    return repo_dir


def execute(job: Job, out_dir: Path, backend: str = "local",
            repo_dir: Path | None = None) -> Result:
    """模式分派。所有模式共享约定：worker 无 GitHub 写令牌；结果只写 out_dir。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    errs = job.validate()
    if errs:
        return Result(job_id=job.job_id, backend=backend, outcome="blocked",
                      base_sha=job.base_sha, unverified_items=errs)
    if repo_dir is not None and job.mode in {"scan", "reproduce"}:
        try:
            ensure_repo(repo_dir, job.repo, job.base_sha)
        except (subprocess.SubprocessError, OSError) as e:
            return Result(job_id=job.job_id, backend=backend, outcome="failed",
                          base_sha=job.base_sha,
                          unverified_items=[f"repo fetch/checkout failed: {e}"])
    if job.mode == "scan":
        from .scan import run_scan
        return run_scan(job, repo_dir or out_dir, out_dir,
                        ruleset=job.ruleset or "p/python")
    if job.mode == "reproduce":
        from .reproduce import run_reproduce
        return run_reproduce(job, repo_dir or out_dir, out_dir,
                             test_cmd=list(job.test_cmd))
    if job.mode == "repair":
        if repo_dir is None:
            return Result(job_id=job.job_id, backend=backend, outcome="blocked",
                          base_sha=job.base_sha,
                          unverified_items=["repair requires --repo-dir"])
        from .repair import run_repair
        return run_repair(job, repo_dir, out_dir, test_cmd=list(job.test_cmd),
                          context=job.context,
                          extra_test_files=dict(job.ai_test_files or {}))
    if job.mode in {"review", "ci"}:
        return _run_repair_like(job, out_dir, backend)
    return Result(job_id=job.job_id, backend=backend, outcome="blocked",
                  base_sha=job.base_sha, unverified_items=[f"unsupported mode {job.mode}"])


def _run_repair_like(job: Job, out_dir: Path, backend: str) -> Result:
    # review/ci：P5 后段接入项目工作流。当前骨架，如实声明未验证。
    return Result(job_id=job.job_id, backend=backend, outcome="inconclusive",
                  base_sha=job.base_sha,
                  unverified_items=[f"{job.mode} engine not implemented yet; skeleton only"])


def emit_envelope(result: Result, out_dir: Path) -> None:
    """stdout 单行信封：result JSON + 关键 artifact 摘要（无持久卷时的回传通道）。"""
    envelope = {"result": result.to_dict()}
    patch_file = out_dir / "change.patch"
    if patch_file.is_file():
        envelope["patch"] = patch_file.read_text(encoding="utf-8", errors="replace")[:200_000]
    report = out_dir / "test-report.json"
    if result.test_report_path and report.is_file():
        envelope["test_report"] = json.loads(report.read_text(encoding="utf-8"))
    findings = out_dir / "findings.json"
    if findings.is_file():
        envelope["findings"] = json.loads(findings.read_text(encoding="utf-8"))
    print(RESULT_MARKER + json.dumps(envelope, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--job", help="path to job.json (or '-' for stdin)")
    src.add_argument("--job-b64", help="base64-encoded job JSON (sandbox argv channel)")
    ap.add_argument("--out", required=True, help="artifacts output directory")
    ap.add_argument("--repo-dir", help="workspace for the target repo clone")
    ap.add_argument("--backend", default="local", choices=["modal", "github_actions", "local"])
    ap.add_argument("--emit-envelope", action="store_true",
                    help="print single-line result envelope to stdout")
    args = ap.parse_args(argv)

    if args.job_b64:
        job_data = json.loads(base64.b64decode(args.job_b64).decode("utf-8"))
    elif args.job == "-":
        job_data = json.loads(sys.stdin.read())
    else:
        job_data = json.loads(Path(args.job).read_text(encoding="utf-8"))
    if job_data.get("allow_github_write"):
        print("[core] refuse: allow_github_write must be false (workers have no write token)",
              file=sys.stderr)
        return 2
    job = Job.from_dict(job_data)
    out_dir = Path(args.out)

    repo_dir = Path(args.repo_dir) if args.repo_dir else None
    result = execute(job, out_dir, backend=args.backend, repo_dir=repo_dir)
    # 出口前最后一道：结果内路径不得越界
    for p in (result.patch_path, result.test_report_path, result.logs_path):
        if check_patch_path(p):
            print(f"[core] refuse: unsafe artifact path {p!r}", file=sys.stderr)
            return 2
    (out_dir / "result.json").write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
    if args.emit_envelope:
        emit_envelope(result, out_dir)
    print(f"[core] wrote {out_dir / 'result.json'} outcome={result.outcome}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
