#!/usr/bin/env python
"""Bug Hunter 有效性基准（ground-truth 回溯）：用"已被维护者合并的 bugfix PR"当已知答案。

原理：选取 MVP 仓库近期合并的 bugfix PR（ground truth = 这确实是 Bug，维护者已修），
把系统拉回修复前的父提交，检验：
  1) semgrep 线索是否命中 PR 改动的文件（检测率）；
  2) OpenCode 看到 diff 后是否判定"这是真 Bug"（确认率）。

这是诚实的有效性度量：不声称发现新 Bug，只度量"已知 Bug 能否被检出"。
成本：每 PR 一次 Modal 扫描（~$0.005），总预算受 BudgetGuard 约束。
用法：python scripts/p6_effectiveness.py --yes [--per-repo 2]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from scout_v2.adapters.modal_backend import ModalSandboxBackend  # noqa: E402
from scout_v2.budget import BudgetGuard, ModalCliBilling  # noqa: E402
from scout_v2.schemas import Job  # noqa: E402

OUT = ROOT / "state" / "bughunt" / "effectiveness"
STOP_THRESHOLD = 27.0


def gh(args: list[str], tries: int = 4) -> str | None:
    for i in range(tries):
        p = subprocess.run(["gh", "api"] + args, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if p.returncode == 0:
            return p.stdout
        time.sleep(min(30, (i + 1) * 8))
    print(f"    [gh fail] {args[0] if args else ''}", file=sys.stderr)
    return None


def pick_bugfix_prs(repo: str, n: int) -> list[dict]:
    """合并的 bugfix PR：标题含 fix、近期、Python 改动 ≤5 文件。"""
    q = f"repo:{repo} is:pr is:merged merged:>=2026-08-01 fix in:title"
    out = gh(["search/issues", "-X", "GET", "-f", f"q={q}", "-f", "per_page=15",
              "-f", "sort=created", "-f", "order=desc"])
    if not out:
        return []
    picked = []
    for it in json.loads(out).get("items", []):
        if len(picked) >= n:
            break
        num = it["number"]
        detail = gh([f"repos/{repo}/pulls/{num}"])
        files = gh([f"repos/{repo}/pulls/{num}/files?per_page=20"])
        if not detail or not files:
            continue
        d = json.loads(detail)
        fl = json.loads(files)
        py_files = [f for f in fl if f["filename"].endswith(".py")]
        if not py_files or len(fl) > 5 or d["additions"] > 60:
            continue  # 只要小而清晰的 bugfix
        picked.append({
            "number": num, "title": it["title"][:90], "url": it["html_url"],
            "base_sha": d["base"]["sha"], "merged_at": d.get("merged_at", ""),
            "files": [{"filename": f["filename"], "patch": f.get("patch", "")[:2500]}
                      for f in py_files[:3]],
        })
    return picked


def modal_scan(backend: ModalSandboxBackend, repo: str, sha: str) -> dict:
    job = Job(mode="scan", repo=repo, base_sha=sha,
              idempotency_key=f"scan:{repo}:{sha[:12]}:effbench",
              execution_scope="broad_scan", timeout_seconds=900,
              cpu=2, memory_mb=4096, max_estimated_compute_usd=0.5,
              ruleset="p/python")
    rid = backend.submit(job.to_dict())
    t0 = time.time()
    while True:
        st = backend.poll(rid)
        if st["state"] != "running":
            break
        if time.time() - t0 > 900:
            backend.cancel(rid)
            return {"error": "watchdog timeout"}
        time.sleep(15)
    env = backend.collect(rid)
    return env.get("findings") or {"findings": []}


def opencode_verdict(repo: str, sha: str, pr: dict, workdir: Path) -> dict:
    """OpenCode 只读判断：这是不是一个真 Bug（ground truth：维护者已修复）。"""
    from scout_v2.workers.opencode_client import OpenCodeClient
    subprocess.run(["git", "clone", "--quiet", "--depth", "1", "--filter=blob:none",
                    f"https://github.com/{repo}.git", str(workdir)],
                   check=True, capture_output=True, timeout=600)
    subprocess.run(["git", "-C", str(workdir), "fetch", "--quiet", "--depth", "1",
                    "origin", sha], capture_output=True, timeout=300)
    subprocess.run(["git", "-C", str(workdir), "checkout", "--quiet", "FETCH_HEAD"],
                   capture_output=True, timeout=120)
    diff_blocks = "\n\n".join(
        f"--- {f['filename']} ---\n{f['patch']}" for f in pr["files"])
    prompt = (
        f"You are reviewing a diff in {repo} (commit {sha[:12]}). This change was "
        "LATER MERGED by maintainers as a bug fix, so a real bug existed here.\n\n"
        f"{diff_blocks}\n\n"
        "Answer in <=5 lines: (1) verdict: real-bug or not-obvious; (2) which file/line "
        "the bug is in; (3) one-line root cause. Do NOT modify files.")
    client = OpenCodeClient(timeout_seconds=300, workdir=workdir)
    ok, detail = client.run("opencode-go/deepseek-v4.1-flash", prompt)
    low = detail.lower() if ok else ""
    verdict = "real_bug" if ok and ("real bug" in low or "verdict: real" in low
                                    or "yes" in low[:200]) else "unclear" if ok else "error"
    return {"ok": ok, "verdict": verdict, "analysis": detail[:800]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--per-repo", type=int, default=2)
    args = ap.parse_args()
    v2 = yaml.safe_load((ROOT / "configs" / "v2.yaml").read_text(encoding="utf-8"))
    repos = v2["bug_hunter"]["mvp_repos"][:3]

    billing = ModalCliBilling()
    metered = billing.metered_usage_usd()
    print(f"[eff] metered at start: {metered}")
    if metered is None or metered >= STOP_THRESHOLD:
        print("[eff] billing unavailable/over threshold — fail-closed")
        return 2
    if not args.yes:
        print(f"[eff] dry-run: would benchmark {repos} ({args.per_repo} PRs each)")
        return 0

    guard = BudgetGuard(soft_cap_usd=min(25.0, STOP_THRESHOLD - metered), enabled=True,
                        billing=billing, max_concurrent=1)
    backend = ModalSandboxBackend(guard, dockerfile=ROOT / "docker" / "Dockerfile.worker",
                                  max_task_minutes=15)
    results = []
    for repo in repos:
        prs = pick_bugfix_prs(repo, args.per_repo)
        print(f"[eff] {repo}: {len(prs)} qualifying bugfix PRs")
        for pr in prs:
            rec = {"repo": repo, "number": pr["number"], "title": pr["title"],
                   "base_sha": pr["base_sha"]}
            try:
                fd = modal_scan(backend, repo, pr["base_sha"])
                findings = fd.get("findings", [])
                changed = {f["filename"] for f in pr["files"]}
                hits = [f for f in findings
                        if f.get("path") in changed
                        or Path(f.get("path", "")).name in {Path(c).name for c in changed}]
                rec["semgrep_total"] = len(findings)
                rec["semgrep_hits_changed_files"] = len(hits)
                rec["detected"] = bool(hits)
                rec["hit_detail"] = [{k: f.get(k) for k in ("path", "line", "rule_id")}
                                     for f in hits[:3]]
            except Exception as e:  # noqa: BLE001
                rec["semgrep_error"] = str(e)[:250]
                results.append(rec)
                continue
            try:
                wd = Path(tempfile.mkdtemp(prefix="eff-")) / "src"
                rec["opencode"] = opencode_verdict(repo, pr["base_sha"], pr, wd)
            except Exception as e:  # noqa: BLE001
                rec["opencode"] = {"ok": False, "verdict": "error", "analysis": str(e)[:250]}
            results.append(rec)
            print(f"  PR#{pr['number']}: detected={rec.get('detected')} "
                  f"opencode={rec.get('opencode', {}).get('verdict')}")
            time.sleep(2)

    total = [r for r in results if "detected" in r]
    summary = {
        "benchmark": "ground-truth regression (merged bugfix PRs, scanned pre-fix)",
        "pr_count": len(total),
        "semgrep_detected": sum(1 for r in total if r["detected"]),
        "opencode_real_bug": sum(1 for r in results
                                 if r.get("opencode", {}).get("verdict") == "real_bug"),
        "metered_after": billing.metered_usage_usd(),
        "results": results,
        "finished": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "effectiveness.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "results"},
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
