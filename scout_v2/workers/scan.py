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


def _snippet(repo_dir: Path, rel_path: str, line: int, context: int = 15) -> str:
    """取命中行附近代码（带行号）。文件读不到返回空串，不影响扫描结果。"""
    try:
        f = repo_dir / rel_path
        if not f.is_file():
            return ""
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        lo = max(0, line - context - 1)
        hi = min(len(lines), line + context)
        return "\n".join(f"{i + lo + 1}: {ln}" for i, ln in enumerate(lines[lo:hi]))[:3000]
    except OSError:
        return ""


def run_scan(job: Job, repo_dir: Path, out_dir: Path, *,
             ruleset: str = "p/python", max_findings: int = 50,
             categories: tuple[str, ...] | None = ("correctness",),
             semgrep_bin: str = "semgrep", runner=None) -> Result:
    """对固定 SHA 的仓库跑精选规则集，产出 findings.json + Result。

    categories：只保留这些 semgrep 类别的 findings。2026-10-31 实测 p/python
    全集扫 3 个仓库出 14 条，AI 抽查 3 条全是 security 噪音（MD5/0o755/XXE）；
    我们要的是行为缺陷（correctness），安全审计类直接在结果层丢掉——semgrep
    每条 result 的 extra.metadata.category 标了类别，无该字段的保留（宁多勿漏，
    由下游 AI 分析再过滤）。
    """
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
    dropped_by_category = 0
    try:
        raw = json.loads(stdout)
        for r in (raw.get("results") or []):
            path = r.get("path") or ""
            check_id = r.get("check_id") or ""
            line = int((r.get("start") or {}).get("line") or 0)
            meta = (r.get("extra") or {}).get("metadata") or {}
            category = str(meta.get("category") or "")
            if categories and category and category not in categories:
                dropped_by_category += 1
                continue
            if len(findings) >= max_findings:
                break
            fp = bug_fingerprint(job.repo, path, check_id, line)
            # 连带代码片段一起回传：Sandbox 里网络通畅、仓库已在本地，
            # 这样下游 OpenCode 分析无需再 clone（部署机可能连不上 GitHub）
            findings.append({
                "path": str(PurePosixPath(path)),
                "line": line,
                "rule_id": check_id,
                "category": category,
                "message": (r.get("extra") or {}).get("message", "")[:300],
                "fingerprint": fp,
                "candidate_id": bug_candidate_id(job.repo, job.base_sha, fp),
                "snippet": _snippet(repo_dir, path, line, context=15),
            })
    except (json.JSONDecodeError, ValueError, AttributeError) as e:
        return Result(job_id=job.job_id, backend="local", outcome="failed",
                      base_sha=job.base_sha, logs_path=rel("worker.log"),
                      unverified_items=[f"unparseable semgrep output: {e}"])

    (out_dir / "findings.json").write_text(
        json.dumps({"schema_version": 2, "job_id": job.job_id, "repo": job.repo,
                    "base_sha": job.base_sha, "ruleset": ruleset,
                    "categories": list(categories or []),
                    "dropped_by_category": dropped_by_category,
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
