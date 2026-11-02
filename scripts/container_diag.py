"""诊断输出：容器健康 + 凭据接线 + pr-fix 派发审计（只打印布尔与摘要）。

被 diagnose.yml 调用。2026-10-31 仓库转公开后，token 从
/data/configs/cloudflare.yaml 移到了环境变量（CF_API_TOKEN / CF_INGEST_TOKEN），
这里验证接线是否正确。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import yaml


def _pr_watch_tail() -> None:
    """最近 10 条 pr_watch 审计（pr-fix 派发/草拟结果在这里）。"""
    try:
        lines = Path("/data/state/worklog.jsonl").read_text(
            encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    hits = [ln for ln in lines if '"pr_fix' in ln or '"pr_needs_reply"' in ln
            or '"pr_reply' in ln]
    print("--- pr_watch audit tail ---")
    for ln in hits[-10:]:
        try:
            r = json.loads(ln)
            print(f"  {r.get('ts')} {r.get('action')} {r.get('target')} :: "
                  f"{str(r.get('result'))[:110]}")
        except Exception:  # noqa: BLE001
            print(" ", ln[-140:])


def main() -> int:
    f = yaml.safe_load(open("/data/configs/cloudflare.yaml", encoding="utf-8")) or {}
    print("file_token_empty:", f.get("api_token") in ("", None))
    print("env_api_set:", bool(os.environ.get("CF_API_TOKEN")))
    print("env_ingest_set:", bool(os.environ.get("CF_INGEST_TOKEN")))
    sys.path.insert(0, "/app")
    from dashboard import data as D

    class _DS:
        root = Path("/data")

    cfg = D.cloudflare_cfg(_DS())
    print("effective_api_set:", bool(cfg.get("api_token")))
    print("effective_ingest_set:", bool(cfg.get("ingest_token")))
    # PR 评论通道的前提：Worker 能读到 PR 列表（dashboard 每 15 分钟写 KV）
    sync = Path("/data/state/pr_sync.json")
    try:
        prs = json.loads(sync.read_text(encoding="utf-8"))
        print("pr_sync_at:", prs.get("at"))
        print("pr_sync_count:", len(prs.get("prs", [])))
    except Exception:  # noqa: BLE001
        print("pr_sync_count: 0")
    _pr_watch_tail()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
