#!/usr/bin/env python
"""P5 完整贡献闭环演练（只在 xixi-box/oss-scout-v2-test 上，不碰任何外部仓库）。

流程：
  1. 埋 bug：向测试仓库 push demo bug commit（calc.py 整除 bug + 回归测试）。
  2. 合规门：PolicyEngine.authorize('push'/'open_pr', ...)（测试仓库已在 repositories.yaml）。
  3. repair：OpenCode(deepseek-v4.1-flash) 产补丁 → base 失败 + 补丁后通过的证据链。
  4. 质量闸：verify_result（SHA/路径/证据链）。
  5. 外部写：push fix/<sha 短> 分支 → gh pr create（自仓库，零外部声誉风险）。

用法：python scripts/p5_drill.py --yes
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

from scout_v2.repo_policy import PolicyEngine  # noqa: E402
from scout_v2.schemas import Job  # noqa: E402
from scout_v2.workers.quality_gate import verify_result  # noqa: E402
from scout_v2.workers.repair import run_repair  # noqa: E402

REPO = "xixi-box/oss-scout-v2-test"
BUG_DIV = "def div(a, b):\n    return a // b\n"
CALC = "def div(a, b):\n    return a / b\n"
TEST = "from calc import div\n\ndef test_div():\n    assert div(7, 2) == 3.5\n"
URL = f"https://github.com/{REPO}.git"


def gh_retry(args: list[str], tries: int = 5) -> str:
    for i in range(tries):
        p = subprocess.run(["gh"] + args, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if p.returncode == 0:
            return p.stdout
        err = p.stderr
        print(f"    [gh retry {i+1}] {err.strip()[-120:]}")
        time.sleep(min(45, (i + 1) * 10))
    raise RuntimeError(f"gh failed: {args}")


def step1_plant_bug(clone: Path) -> str:
    """提交并 push demo bug（幂等：已埋过则直接用远端 HEAD）。返回 base_sha。"""
    (clone / "calc.py").write_text(BUG_DIV, encoding="utf-8")
    (clone / "test_div.py").write_text(TEST, encoding="utf-8")
    # PR 不得带构建产物：回归测试在本地跑过会生成 __pycache__
    (clone / ".gitignore").write_text("__pycache__/\n*.pyc\n.pytest_cache/\n", encoding="utf-8")
    st = subprocess.run(["git", "status", "--porcelain"], cwd=clone,
                        capture_output=True, text=True).stdout.strip()
    if not st:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=clone,
                              capture_output=True, text=True).stdout.strip()
    for a in (["add", "-A"], ["-c", "user.name=xixi-box", "-c", "user.email=wangshun-cn@qq.com",
                              "commit", "-qm", "demo: planted div rounding bug for P5 drill"]):
        subprocess.run(["git"] + a, cwd=clone, capture_output=True, text=True, check=True)
    for i in range(5):
        p = subprocess.run(["git", "push", "origin", "main"], cwd=clone,
                           capture_output=True, text=True)
        if p.returncode == 0:
            break
        print(f"    [push retry {i+1}] {p.stderr.strip()[-120:]}")
        time.sleep(min(45, (i + 1) * 10))
    else:
        raise RuntimeError("push failed after retries")
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=clone,
                          capture_output=True, text=True).stdout.strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="真的执行：push demo bug + 创建 PR")
    args = ap.parse_args()
    if not args.yes:
        print("[drill] dry-run only; pass --yes to execute (own test repo only)")
        return 0

    # ---- 0. 合规门 ----
    eng, err = PolicyEngine.load(ROOT)
    assert err is None, f"AGENTS.md unreadable: {err}"
    for action in ("push", "open_pr", "claim"):
        d = eng.authorize(action, REPO)
        print(f"[gate] authorize {action}: allowed={d.allowed} ({d.reason})")
        assert d.allowed, f"policy gate blocked {action}: {d.reason}"

    # ---- 1. 埋 bug ----
    tmp = Path(tempfile.mkdtemp(prefix="p5drill-"))
    clone = tmp / "clone"
    subprocess.run(["git", "clone", "--quiet", URL, str(clone)], check=True)
    base_sha = step1_plant_bug(clone)
    print(f"[drill] bug planted, base_sha={base_sha[:12]}")

    # ---- 2-3. repair（真实 OpenCode）----
    out = ROOT / "artifacts" / f"p5-{int(time.time())}"
    job = Job(mode="repair", repo=REPO, base_sha=base_sha,
              idempotency_key=f"repair:{REPO}:{base_sha}:p5drill",
              test_cmd=[sys.executable, "-m", "pytest", "test_div.py", "-q"],
              context=("Function div(a,b) in calc.py returns integer division (a // b) "
                       "but the documented contract and test_div.py require true division "
                       "(7/2 == 3.5). Fix the implementation, not the test."))
    res = run_repair(job, clone, out, test_cmd=list(job.test_cmd), context=job.context)
    print(f"[repair] outcome={res.outcome} reproduced_on_base={res.reproduced_on_base} "
          f"fixed_on_patch={res.fixed_on_patch}")

    # ---- 4. 质量闸 ----
    report = verify_result(res, job)
    print(f"[quality] {'PASS' if report.ok else report.errors}")
    assert report.ok, report.errors

    # ---- 5. 外部写：分支 + PR（自仓库）----
    if not (res.outcome == "succeeded" and res.fixed_on_patch and res.reproduced_on_base):
        print("[drill] evidence chain incomplete → no PR (fail-closed)")
        return 2
    patch_file = out / "change.patch"
    branch = f"fix/p5-drill-div-{base_sha[:7]}"
    subprocess.run(["git", "checkout", "-q", base_sha], cwd=clone, check=True)
    subprocess.run(["git", "checkout", "-q", "-b", branch], cwd=clone, check=True)
    with patch_file.open("r", encoding="utf-8") as f:
        ap = subprocess.run(["git", "apply", "--whitespace=nowarn", "-"], cwd=clone,
                            stdin=f, capture_output=True, text=True)
    assert ap.returncode == 0, ap.stderr
    subprocess.run(["git", "add", "-A"], cwd=clone, check=True)
    subprocess.run(["git", "-c", "user.name=xixi-box", "-c", "user.email=wangshun-cn@qq.com",
                    "commit", "-qm", "fix: use true division in div()"], cwd=clone, check=True)
    for i in range(5):
        p = subprocess.run(["git", "push", "-q", "origin", branch], cwd=clone,
                           capture_output=True, text=True)
        if p.returncode == 0:
            break
        print(f"    [push retry {i+1}] {p.stderr.strip()[-120:]}")
        time.sleep(min(45, (i + 1) * 10))
    else:
        raise RuntimeError("branch push failed")
    body = (f"What: change `div()` from integer division to true division.\n\n"
            f"Root cause: implementation used `//` while the contract (docstring contract in "
            f"test_div.py) requires `7 / 2 == 3.5`.\n\n"
            f"How: one-line change in calc.py.\n\n"
            f"Tests: test_div.py fails on base commit {base_sha[:12]}, passes after patch "
            f"(evidence in artifacts).")
    pr_url = gh_retry(["pr", "create", "--repo", REPO, "--base", "main", "--head", branch,
                       "--title", "fix: use true division in div()", "--body", body]).strip()
    print(f"[drill] PR created: {pr_url}")
    print(f"[drill] artifacts at {out}")
    (out / "pr_url.txt").write_text(pr_url, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
