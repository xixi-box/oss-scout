"""诊断 2：为什么 pipeline 里的 clone 会 129s 超时，而独立执行只要 2s。

    python scripts/diag_proxy2.py

逐步逼近：先裸 clone，再模拟 pipeline 的前置步骤（_no_proxy_context +
子进程 gh 调用 + 长时间等待），每一步后再 clone 一次，看哪一步破坏了连通性。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "/app")
from scout_v2.workers import auto_pipeline as ap  # noqa: E402

REPO = "xixi-box/oss-scout-v2-test"


def probe(label: str, timeout: int = 140) -> bool:
    tok = (os.environ.get("GH_TOKEN") or "").strip()
    url = (f"https://x-access-token:{tok}@github.com/{REPO}.git" if tok
           else f"https://github.com/{REPO}.git")
    dest = Path("/tmp") / f"dp2-{label.replace(' ', '_')}"
    subprocess.run(["rm", "-rf", str(dest)], capture_output=True)
    start = time.time()
    try:
        r = subprocess.run(
            ["git", "clone", "--quiet", "--filter=blob:none", "--depth", "1", url, str(dest)],
            capture_output=True, text=True, timeout=timeout)
        ok = r.returncode == 0
        tail = (r.stderr or "").strip()[-140:]
    except subprocess.TimeoutExpired:
        ok, tail = False, f"TIMEOUT >{timeout}s"
    print(f"[{label}] ok={ok} elapsed={round(time.time() - start, 1)}s "
          f"proxy={os.environ.get('HTTPS_PROXY')} {tail}", flush=True)
    subprocess.run(["rm", "-rf", str(dest)], capture_output=True)
    return ok


def main() -> int:
    probe("1_baseline")

    print("--- 模拟 pipeline 前置：_no_proxy_context 内跑 gh 子进程 ---", flush=True)
    with ap._no_proxy_context():
        subprocess.run(["gh", "api", f"repos/{REPO}/commits/HEAD", "--jq", ".sha"],
                       capture_output=True, text=True, timeout=120)
    probe("2_after_gh_in_noproxy")

    print("--- 模拟 _collect 的轮询等待 ---", flush=True)
    for _ in range(6):
        time.sleep(10)
    probe("3_after_sleep")

    print("--- 再跑一次 complete_pipeline 实际用的 _git_retry ---", flush=True)
    tok = (os.environ.get("GH_TOKEN") or "").strip()
    url = (f"https://x-access-token:{tok}@github.com/{REPO}.git" if tok
           else f"https://github.com/{REPO}.git")
    dest = "/tmp/dp2-retry"
    subprocess.run(["rm", "-rf", dest], capture_output=True)
    start = time.time()
    cl = ap._git_retry(["clone", "--quiet", "--filter=blob:none", "--depth", "1",
                        url, dest], timeout=140)
    print(f"[4_git_retry] rc={cl.returncode} elapsed={round(time.time() - start, 1)}s "
          f"{(cl.stderr or '')[-140:]}", flush=True)
    subprocess.run(["rm", "-rf", dest], capture_output=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
