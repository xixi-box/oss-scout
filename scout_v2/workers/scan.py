"""Bug Hunter 一级产出：Semgrep 扫描 → 可疑线索 → bug 候选（计划 §8）。

定位：静态线索**不是**真 Bug 证据；只产生带指纹的 suspected 候选，
升级为 reproduced 必须经过 reproduce.py 的隔离执行（原始版失败才算）。

设计为 local/Modal 共用：repo_dir 由调用方准备（本地缓存或 Sandbox 内按 SHA 拉取）。
"""

from __future__ import annotations

import hashlib
import json
import re
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


# xfail/skip 标记：仓库**自己承认**的已知缺陷。这是密度最高的信号源——
# 有人写了"这里有问题但我暂时不修"，且自带复现方式（那条被标记的测试）。
# 五个独立模式，各自一个 reason 组；比一个大正则的可选前缀好排查得多
# （第一版把标记名 "xfail" 当 reason 捕获，2026-11-01 实测）。
_XFAIL_RES = [
    # @pytest.mark.xfail(...reason="...") / @pytest.mark.skip(reason="...")
    (re.compile(r'@pytest\.mark\.(xfail|skip)[^\n"]*?["\']([^"\']{8,})["\']'),
     lambda m: m.group(1), lambda m: m.group(2)),
    # pytest.xfail("...")
    (re.compile(r'pytest\.xfail\s*\(\s*["\']([^"\']{8,})["\']'),
     lambda m: "xfail", lambda m: m.group(1)),
    # @unittest.skip("...") / skipIf(cond, "...") / skipUnless(...)
    (re.compile(r'@(?:unittest\.)?skip(?:If|Unless)?\s*\([^)]*?["\']([^"\']{8,})["\']'),
     lambda m: "skip", lambda m: m.group(1)),
    # self.skipTest("...")
    (re.compile(r'self\.skipTest\s*\(\s*["\']([^"\']{8,})["\']'),
     lambda m: "skip", lambda m: m.group(1)),
]


def mine_xfail(repo_dir: Path, max_findings: int = 40) -> list[dict]:
    """扫测试文件里的 xfail/skip 标记，产出"已知缺陷"线索。

    只认带 reason 的标记（无 reason 的 skip 多半是平台跳过，不是缺陷）。
    返回与 semgrep findings 同构的 dict 列表（rule_id 用 xfail-marker/ 前缀），
    合并进 findings 后走同一条下游分析管道。
    """
    out: list[dict] = []
    try:
        candidates = [p for p in repo_dir.rglob("*.py")
                      if p.name.startswith("test_") or p.name.endswith("_test.py")
                      or "tests" in p.parts or "test" in p.parts]
    except OSError:
        return out
    for path in candidates[:400]:
        try:
            rel = str(path.relative_to(repo_dir)).replace("\\", "/")
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for i, ln in enumerate(lines):
            kind = reason = None
            for pat, kind_fn, reason_fn in _XFAIL_RES:
                m = pat.search(ln)
                if m:
                    kind = kind_fn(m)
                    reason = reason_fn(m).strip()
                    break
            if not kind or not reason:
                continue          # 无 reason 的 skip 多半是平台跳过
            fp = bug_fingerprint("xfail", rel, f"{kind}:{reason[:40]}", i + 1)
            out.append({
                "path": rel,
                "line": i + 1,
                "rule_id": f"xfail-marker/{kind}",
                "category": "correctness",
                "message": f"acknowledged {kind}: {reason[:200]}",
                "fingerprint": fp,
                "candidate_id": bug_candidate_id("xfail", "HEAD", fp),
                "snippet": "\n".join(
                    f"{j + 1}: {lines[j]}" for j in range(max(0, i - 3), min(len(lines), i + 4))
                )[:1500],
            })
            if len(out) >= max_findings:
                return out
    return out


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

    # 第二信号源：xfail/skip 挖掘。仓库自己承认的缺陷，密度高于任何静态规则；
    # 不受 categories 过滤（它们不是 semgrep 结果）。semgrep 命中排前面。
    xfail_hits = mine_xfail(repo_dir, max_findings=max(max_findings // 2, 20))
    findings.extend(xfail_hits)

    (out_dir / "findings.json").write_text(
        json.dumps({"schema_version": 2, "job_id": job.job_id, "repo": job.repo,
                    "base_sha": job.base_sha, "ruleset": ruleset,
                    "categories": list(categories or []),
                    "dropped_by_category": dropped_by_category,
                    "xfail_count": len(xfail_hits),
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
