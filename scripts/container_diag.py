"""诊断输出：容器健康 + 凭据接线（只打印布尔，不打印值）。

被 diagnose.yml 调用。2026-10-31 仓库转公开后，token 从
/data/configs/cloudflare.yaml 移到了环境变量（CF_API_TOKEN / CF_INGEST_TOKEN），
这里验证接线是否正确。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml


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
    # PR 评论通道的前提：Worker 能读到 PR 列表
    sync = Path("/data/state/pr_sync.json")
    try:
        prs = json_prs = __import__("json").loads(sync.read_text(encoding="utf-8"))
        print("pr_sync_count:", len(prs.get("prs", [])))
    except Exception:  # noqa: BLE001
        print("pr_sync_count: 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
