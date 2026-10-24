#!/usr/bin/env python
"""Modal Sandbox 手动冒烟测试（P2 验收；默认拒绝运行，需显式 --yes）。

前置条件（计划 §12 + 用户控制台确认 2026-10-25）：
  1. modal CLI 已装且已 `modal token set`（或环境变量 MODAL_TOKEN_ID/SECRET）。
  2. 平台硬限制已在 configs/v2.yaml 记录（usage_limit $30 / spend_limit $0）。
  3. BudgetGuard 软阈值 $25/月、单并发、≤30 分钟。

流程：账单读数 → 预算闸 → 提交一个最小 scan job（MVP 名单第一个仓库，p/python，
15 分钟上限）→ 轮询 → 收集 stdout 信封 → terminate Sandbox → 打印结论。

用法：python scripts/modal_smoke.py --yes
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from scout_v2.adapters.modal_backend import (BackendDisabledError,  # noqa: E402
                                             BudgetExceededError, ModalSandboxBackend)
from scout_v2.budget import BudgetGuard, ModalCliBilling  # noqa: E402
from scout_v2.schemas import Job  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="真的会创建 Sandbox 并计量费用")
    ap.add_argument("--repo", default="openai/openai-agents-python")
    args = ap.parse_args()

    v2 = yaml.safe_load((ROOT / "configs" / "v2.yaml").read_text(encoding="utf-8"))
    mb = v2["budget"]["modal"]
    hard = mb.get("platform_hard_limits") or {}
    if not hard.get("spend_limit_usd") == 0 or not hard.get("usage_limit_usd"):
        print("[smoke] platform hard limits not confirmed in v2.yaml — refusing")
        return 2
    if not args.yes:
        print("[smoke] DRY RUN — all preconditions look present. Re-run with --yes "
              "to create a real Sandbox (billed against included credits).")
        print(f"[smoke] would scan: {args.repo} ruleset={mb.get('semgrep_ruleset')} "
              f"cap={mb['max_task_minutes']}min soft_cap=${mb['monthly_soft_cap_usd']}")
        return 0

    billing = ModalCliBilling()
    if not billing.healthy():
        print("[smoke] billing source unhealthy (`modal billing summary --json` failed) "
              "— fail-closed, no sandbox")
        return 2
    metered = billing.metered_usage_usd()
    print(f"[smoke] metered usage this period: ${metered}")

    guard = BudgetGuard(soft_cap_usd=mb["monthly_soft_cap_usd"], enabled=True,
                        billing=billing, max_concurrent=mb["max_concurrent_sandboxes"])
    backend = ModalSandboxBackend(guard, dockerfile=ROOT / "docker" / "Dockerfile.worker",
                                  max_task_minutes=min(mb["max_task_minutes"], 15))
    job = Job(mode="scan", repo=args.repo, base_sha="HEAD",
              idempotency_key=f"smoke:{args.repo}:{int(time.time())}",
              execution_scope="broad_scan", timeout_seconds=15 * 60,
              cpu=2, memory_mb=4096, max_estimated_compute_usd=0.5,
              ruleset=mb.get("semgrep_ruleset", "p/python"))
    # 冒烟用 HEAD 而非固定 SHA：只验证链路，产物不作为候选入库
    errs = job.validate()
    if errs:
        print(f"[smoke] invalid job: {errs}")
        return 2

    try:
        remote_id = backend.submit(job.to_dict())
    except (BackendDisabledError, BudgetExceededError) as e:
        print(f"[smoke] submit refused (budget gate working): {e}")
        return 2
    print(f"[smoke] sandbox submitted: {remote_id}; polling…")
    while True:
        status = backend.poll(remote_id)
        if status["state"] != "running":
            break
        time.sleep(15)
    print(f"[smoke] sandbox finished: {status}")
    out = backend.collect(remote_id)
    print(json.dumps(out, ensure_ascii=False, indent=1)[:3000])
    verdict = "ENVELOPE OK" if "result" in out else "NO ENVELOPE (adapter needs fixing)"
    print(f"[smoke] verdict: {verdict}; sandbox terminated; reservation released")
    return 0 if "result" in out else 1


if __name__ == "__main__":
    raise SystemExit(main())
