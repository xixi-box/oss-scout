"""直接调试 Modal repair 通道：打印 collect 的完整返回（含 stdout_tail）。

    python scripts/modal_repair_probe.py
"""
from __future__ import annotations

import json
import os
import sys
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
    head = ap.subprocess.run(["gh", "api", f"repos/{REPO}/commits/HEAD", "--jq", ".sha"],
                             capture_output=True, text=True, timeout=120)
    sha = head.stdout.strip()
    print("sha:", sha)

    job = Job(mode="repair", repo=REPO, base_sha=sha,
              idempotency_key=f"dbg-repair:{sha}",
              execution_scope="project_contribution",
              policy_snapshot_id=eng.snapshot_id(),
              context="calc.div(7,2) returns 3.0 but should return 3.5. "
                      "Integer division truncates the result.",
              max_estimated_compute_usd=0.5,
              test_cmd=["python", "-m", "pytest", "test_ai_regression.py", "-q"],
              ai_test_files={"test_ai_regression.py": (
                  "from calc import div\n\n\n"
                  "def test_div_returns_float_for_non_even_division():\n"
                  "    assert div(7, 2) == 3.5\n")})

    backend, rid, extra = ap._submit_job(ROOT, job, eng)
    print("backend:", backend, "id:", rid)
    collected = ap._collect(ROOT, backend, rid, extra, timeout_s=1500)
    print(json.dumps(collected, ensure_ascii=False, indent=1, default=str)[:6000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
