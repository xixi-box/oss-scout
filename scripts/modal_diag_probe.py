"""诊断 Modal Sandbox 内部为何无信封：打印 returncode / stdout / stderr 全文。"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, "/app")
from scout_v2.repo_policy import PolicyEngine  # noqa: E402
from scout_v2.schemas import Job  # noqa: E402
from scout_v2.workers import auto_pipeline as ap  # noqa: E402

ROOT = Path("/data")
REPO = "xixi-box/oss-scout-v2-test"


def main() -> int:
    eng, err = PolicyEngine.load(ROOT)
    assert err is None, err
    sha = ap.subprocess.run(["gh", "api", f"repos/{REPO}/commits/HEAD", "--jq", ".sha"],
                            capture_output=True, text=True, timeout=120).stdout.strip()
    job = Job(mode="repair", repo=REPO, base_sha=sha,
              idempotency_key=f"dbg2:{sha}", execution_scope="project_contribution",
              policy_snapshot_id=eng.snapshot_id(),
              context="calc.div(7,2) returns 3.0 but should return 3.5.",
              max_estimated_compute_usd=0.5,
              test_cmd=["python", "-m", "pytest", "test_ai_regression.py", "-q"],
              model="opencode-go/deepseek-v4.1-flash",
              ai_test_files={"test_ai_regression.py":
                             "from calc import div\n\ndef test_x():\n    assert div(7,2)==3.5\n"})

    backend, rid, extra = ap._submit_job(ROOT, job, eng)
    be = extra["backend_obj"]
    print("backend:", backend, "id:", rid, flush=True)
    t0 = time.time()
    with ap._no_proxy_context():
        while True:
            st = be.poll(rid)
            print(f"poll {int(time.time()-t0)}s: {st}", flush=True)
            if st.get("state") != "running":
                break
            if time.time() - t0 > 1500:
                break
            time.sleep(20)
        sb = be._sandboxes[rid]
        out = sb.stdout.read()
        errout = sb.stderr.read()
    print("=== rc:", st.get("returncode"), "stdout len:", len(out), "stderr len:", len(errout))
    print("=== STDOUT ===")
    print(out[-3000:])
    print("=== STDERR ===")
    print(errout[-4000:])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
