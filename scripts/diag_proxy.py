"""诊断：pipeline 在 push 阶段连不上 github.com 的原因。

    python scripts/diag_proxy.py

对比"裸环境"和"经过 _no_proxy_context 之后"两种状态的 git 连通性。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "/app")
from scout_v2.workers import auto_pipeline as ap  # noqa: E402


def probe(label: str, timeout: int = 60) -> None:
    tok = (os.environ.get("GH_TOKEN") or "").strip()
    url = (f"https://x-access-token:{tok}@github.com/xixi-box/oss-scout-v2-test.git"
           if tok else "https://github.com/xixi-box/oss-scout-v2-test.git")
    dest = Path("/tmp") / f"ct-{label}"
    subprocess.run(["rm", "-rf", str(dest)], capture_output=True)
    start = time.time()
    try:
        r = subprocess.run(
            ["git", "clone", "--quiet", "--filter=blob:none", "--depth", "1", url, str(dest)],
            capture_output=True, text=True, timeout=timeout)
        rc, err = r.returncode, (r.stderr or "").strip()[-160:]
    except subprocess.TimeoutExpired:
        rc, err = "TIMEOUT", f">{timeout}s"
    print(f"[{label}] proxy={os.environ.get('HTTPS_PROXY')!r} "
          f"rc={rc} elapsed={round(time.time() - start, 1)}s {err}")
    subprocess.run(["rm", "-rf", str(dest)], capture_output=True)


def main() -> int:
    probe("outside")
    with ap._no_proxy_context():
        probe("inside-no-proxy")
    probe("after")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
