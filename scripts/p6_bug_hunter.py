#!/usr/bin/env python
"""P6-A2：真实 Modal Sandbox Bug Hunter 扫描（3 个 MVP 仓库）。

流程（每仓库）：
  1. 读 HEAD SHA（只读 gh api）。
  2. 预算闸 → 提交 Modal Sandbox scan job（semgrep p/python，≤25 分钟）。
  3. 轮询 → 收集 stdout 信封（result + findings）。
  4. 线索去重：对前 N 条 finding 搜上游 open issues/PRs（只读）。
  5. OpenCode 只读分析首个可疑线索（浅克隆，≤300s）。
  6. 结构化落盘 state/bughunt/<repo>/<sha>/。

不做任何 GitHub 写操作；不复现的线索一律 inconclusive。
用法：python scripts/p6_bug_hunter.py --yes [--repos 2]
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 配置根：容器里运行时配置在 /data/configs（卷持久化），本地开发在项目根
CFG_ROOT = ROOT if (ROOT / "configs" / "v2.yaml").exists() else Path(
    os.environ.get("OSSCOUT_DASHBOARD_ROOT", "/data"))

import yaml  # noqa: E402

from scout_v2.adapters.modal_backend import (BudgetExceededError,  # noqa: E402
                                             ModalSandboxBackend, parse_envelope)
from scout_v2.budget import BudgetGuard, ModalCliBilling  # noqa: E402
from scout_v2.schemas import Job  # noqa: E402

OUT_ROOT = ROOT / "state" / "bughunt"


def gh_retry(args: list[str], tries: int = 4) -> str | None:
    for i in range(tries):
        p = subprocess.run(["gh", "api"] + args, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if p.returncode == 0:
            return p.stdout
        time.sleep(min(40, (i + 1) * 8))
    return None


def head_sha(repo: str) -> str | None:
    out = gh_retry([f"repos/{repo}/commits/HEAD", "--jq", ".sha"])
    return out.strip() if out else None


def dedupe_search(repo: str, finding: dict) -> dict:
    """只读搜索上游是否已有相关 issue/PR。"""
    kw = f'"{Path(finding["path"]).name}"'
    out = gh_retry(["search/issues", "-X", "GET",
                    "-f", f"q=repo:{repo} {kw}",
                    "-f", "per_page=5"])
    hits = []
    if out:
        for it in json.loads(out).get("items", []):
            hits.append({"number": it["number"], "title": it["title"][:90],
                         "is_pr": "pull_request" in it, "state": it["state"],
                         "url": it["html_url"]})
    return {"query": kw, "hits": hits}


def opencode_analyze(repo: str, sha: str, finding: dict, workdir: Path) -> dict:
    """OpenCode 只读分析一个可疑线索（不修改任何文件）。

    代码片段优先取 Sandbox 回传的 finding["snippet"]——部署机常连不上 GitHub，
    在那里 clone 会失败；Sandbox 内网络通畅且仓库已在本地，扫描时顺手带回。
    """
    from scout_v2.workers.opencode_client import OpenCodeClient
    snippet = (finding.get("snippet") or "").strip()
    if not snippet:
        try:
            workdir.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "clone", "--quiet", "--depth", "1", "--filter=blob:none",
                            f"https://github.com/{repo}.git", str(workdir)],
                           check=True, capture_output=True, timeout=180)
            fpath = workdir / finding["path"]
            if fpath.is_file():
                ls = fpath.read_text(encoding="utf-8", errors="replace").splitlines()
                lo = max(0, finding["line"] - 15)
                snippet = chr(10).join(f"{i+lo+1}: {l}" for i, l in
                                       enumerate(ls[lo:finding["line"] + 5]))[:3000]
        except Exception:  # noqa: BLE001 — 无片段也能凭规则描述分析
            snippet = "(code snippet unavailable; analyze from rule + message only)"
    prompt = (
        "You are reviewing a static-analysis finding. Answer in <=6 lines.\n"
        f"Repo: {repo} at commit {sha}\n"
        f"Rule: {finding['rule_id']}\nPath: {finding['path']} line {finding['line']}\n"
        f"Semgrep message: {finding['message']}\n\nCode around the line:\n{snippet}\n\n"
        "Question: is this plausibly a real bug worth a maintainer's time, a false "
        "positive, or needs-more-context? State which test would settle it. "
        "Do NOT modify any files.")
    workdir.mkdir(parents=True, exist_ok=True)   # OpenCodeClient 以 cwd 运行
    client = OpenCodeClient(timeout_seconds=300, workdir=workdir)
    ok, detail = client.run("opencode-go/deepseek-v4.1-flash", prompt)
    return {"ok": ok, "analysis": detail[:1200] if ok else detail[:300]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--repos", type=int, default=3, help="扫描 MVP 名单前 N 个仓库")
    ap.add_argument("--findings-cap", type=int, default=20)
    args = ap.parse_args()
    v2 = yaml.safe_load((CFG_ROOT / "configs" / "v2.yaml").read_text(encoding="utf-8"))
    mb = v2["budget"]["modal"]
    repos = v2["bug_hunter"]["mvp_repos"][:args.repos]
    stop_threshold = 27.0  # 用户约定：$27 或更保守值即停

    billing = ModalCliBilling()
    metered = billing.metered_usage_usd()
    print(f"[bh] metered at start: {metered}")
    if metered is None or metered >= stop_threshold:
        print("[bh] billing unavailable or at/over stop threshold — fail-closed, no sandbox")
        return 2
    if not args.yes:
        print(f"[bh] dry-run: would scan {repos} via Modal (cap {mb['max_task_minutes']}min)")
        return 0

    usage_limit = (mb.get("platform_hard_limits") or {}).get("usage_limit_usd", 30.0)
    guard = BudgetGuard(soft_cap_usd=min(usage_limit, stop_threshold - metered),
                        enabled=True, billing=billing,
                        max_concurrent=mb["max_concurrent_sandboxes"])
    backend = ModalSandboxBackend(guard, dockerfile=ROOT / "docker" / "Dockerfile.worker",
                                  max_task_minutes=mb["max_task_minutes"])
    summary = {"started": time.strftime("%Y-%m-%dT%H:%M:%S"), "repos": [], "errors": []}

    for repo in repos:
        entry: dict = {"repo": repo}
        sha = head_sha(repo)
        if sha is None:
            entry["error"] = "head sha unavailable"
            summary["repos"].append(entry)
            continue
        entry["sha"] = sha
        job = Job(mode="scan", repo=repo, base_sha=sha,
                  idempotency_key=f"scan:{repo}:{sha}:p6",
                  execution_scope="broad_scan", timeout_seconds=1500,
                  cpu=2, memory_mb=4096, max_estimated_compute_usd=0.5,
                  ruleset=mb.get("semgrep_ruleset", "p/python"))
        try:
            remote_id = backend.submit(job.to_dict())
        except (BudgetExceededError, Exception) as e:  # noqa: BLE001
            entry["error"] = f"submit refused/failed: {e}"
            summary["repos"].append(entry)
            continue
        print(f"[bh] {repo} sandbox submitted: {remote_id}")
        t0 = time.time()
        while True:
            st = backend.poll(remote_id)
            if st["state"] != "running":
                break
            if time.time() - t0 > mb["max_task_minutes"] * 60 + 120:
                backend.cancel(remote_id)
                st = {"state": "cancelled locally (watchdog)"}
                break
            time.sleep(20)
        entry["sandbox_state"] = st.get("state")
        env = backend.collect(remote_id)
        dest = OUT_ROOT / repo.replace("/", "__") / sha[:12]
        dest.mkdir(parents=True, exist_ok=True)
        findings = env.get("findings") or {}
        (dest / "result.json").write_text(
            json.dumps(env.get("result", env), ensure_ascii=False, indent=1), encoding="utf-8")
        n = len(findings.get("findings", []))
        entry["findings"] = n
        print(f"[bh] {repo}: {st.get('state')} findings={n} ({time.time()-t0:.0f}s)")
        if n:
            capped = dict(findings)
            capped["findings"] = findings["findings"][:args.findings_cap]
            for f in capped["findings"]:
                f["upstream"] = dedupe_search(repo, f)
            (dest / "findings.json").write_text(
                json.dumps(capped, ensure_ascii=False, indent=1), encoding="utf-8")
            # OpenCode 分析首个线索（每仓库 1 个，控成本）
            if capped["findings"]:
                try:
                    wd = Path(tempfile.mkdtemp(prefix=f"bh-{repo.split('/')[1]}-")) / "src"
                    entry["opencode_analysis"] = opencode_analyze(
                        repo, sha, capped["findings"][0], wd)
                except Exception as e:  # noqa: BLE001
                    entry["opencode_analysis"] = {"ok": False, "analysis": str(e)[:300]}
        summary["repos"].append(entry)
        metered_now = billing.metered_usage_usd()
        entry["metered_after"] = metered_now
        print(f"[bh] metered after {repo}: {metered_now}")
        if metered_now is not None and metered_now >= stop_threshold:
            print("[bh] hit stop threshold — stopping further scans")
            break

    summary["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    (OUT_ROOT / "p6_summary.json").parent.mkdir(parents=True, exist_ok=True)
    (OUT_ROOT / "p6_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
