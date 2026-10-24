"""Bug Hunter 一级产出：Semgrep 扫描 → 可疑线索 → bug 候选（计划 §8）。

定位：静态线索**不是**真 Bug 证据；只产生带指纹的 suspected 候选，
升级为 reproduced 必须经过 reproduce.py 的隔离执行（原始版失败才算）。

设计为 local/Modal 共用：repo_dir 由调用方准备（本地缓存或 Sandbox 内按 SHA 拉取）。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path, PurePosixPath

from ..schemas import Job, Result


def bug_fingerprint(repo: str, path: str, rule_id: str, line: int) -> str:
    return hashlib.sha256(f"{repo}|{path}|{rule_id}|{line}".encode("utf-8")).hexdigest()[:12]


def bug_candidate_id(repo: str, base_sha: str, fingerprint: str) -> str:
    return f"github:{repo}:bug:{base_sha}:{fingerprint}"


def _run(cmd: list[str], cwd: Path | None = None) -> tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=cwd, timeout=1800)
    return p.returncode, p.stdout, p.stderr


def run_scan(job: Job, repo_dir: Path, out_dir: Path, *,
             ruleset: str = "p/python", max_findings: int = 50,
             semgrep_bin: str = "semgrep", runner=None) -> Result:
    """对固定 SHA 的仓库跑精选规则集，产出 findings.json + Result。"""
    runner = runner or _run
    out_dir.mkdir(parents=True, exist_ok=True)
    rel = lambda name: f"artifacts/{job.job_id}/{name}"

    cmd = [semgrep_bin, "--config", ruleset, "--json", "--quiet", str(repo_dir)]
    try:
        rc, stdout, stderr = runner(cmd)
    except FileNotFoundError:
        return Result(job_id=job.job_id, backend="local", outcome="inconclusive",
                      base_sha=job.base_sha,
                      unverified_items=["semgrep binary not available in this environment"])
    except subprocess.TimeoutExpired:
        return Result(job_id=job.job_id, backend="local", outcome="timed_out",
                      base_sha=job.base_sha, unverified_items=["semgrep run exceeded 1800s"])

    (out_dir / "worker.log").write_text(
        f"$ {' '.join(cmd)}\nrc={rc}\n{stderr[-4000:]}\n", encoding="utf-8")

    if rc not in (0, 1):     # semgrep：0=无发现 1=有发现 2+=运行错误
        return Result(job_id=job.job_id, backend="local", outcome="failed",
                      base_sha=job.base_sha,
                      logs_path=rel("worker.log"),
                      unverified_items=[f"semgrep exited {rc}: {stderr[-300:]}"])

    findings = []
    try:
        raw = json.loads(stdout)
        for r in (raw.get("results") or [])[:max_findings]:
            path = r.get("path") or ""
            check_id = r.get("check_id") or ""
            line = int((r.get("start") or {}).get("line") or 0)
            fp = bug_fingerprint(job.repo, path, check_id, line)
            findings.append({
                "path": str(PurePosixPath(path)),
                "line": line,
                "rule_id": check_id,
                "message": (r.get("extra") or {}).get("message", "")[:300],
                "fingerprint": fp,
                "candidate_id": bug_candidate_id(job.repo, job.base_sha, fp),
            })
    except (json.JSONDecodeError, ValueError, AttributeError) as e:
        return Result(job_id=job.job_id, backend="local", outcome="failed",
                      base_sha=job.base_sha, logs_path=rel("worker.log"),
                      unverified_items=[f"unparseable semgrep output: {e}"])

    (out_dir / "findings.json").write_text(
        json.dumps({"schema_version": 1, "job_id": job.job_id, "repo": job.repo,
                    "base_sha": job.base_sha, "ruleset": ruleset,
                    "findings": findings}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    # 误报率统计前，线索一律 inconclusive 而非 succeeded——防止被当成已验证证据
    return Result(job_id=job.job_id, backend="local", outcome="inconclusive",
                  base_sha=job.base_sha,
                  test_report_path=rel("findings.json"),
                  logs_path=rel("worker.log"),
                  unverified_items=[
                      f"{len(findings)} static-analysis clue(s); "
                      "not bug evidence until reproduced on base (plan §8.2.5)"])
