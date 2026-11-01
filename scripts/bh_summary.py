"""汇总最近一轮 bug hunter 扫描的 findings：类别分布与误报率素材。

读 state/bughunt/*/findings.json（schema_version 2，含 category 与
dropped_by_category），打印每仓库的保留/丢弃数量和 rule_id 分布，
作为"correctness 过滤后还剩多少、质量如何"的度量。

用法（容器内）：python /app/scripts/bh_summary.py
"""
from __future__ import annotations

import collections
import json
from pathlib import Path

OUT_ROOT = Path("/data/state/bughunt")


def main() -> int:
    files = sorted(OUT_ROOT.glob("*/*/findings.json"))
    if not files:
        print("no findings files")
        return 0
    total_kept = total_dropped = 0
    rules: collections.Counter = collections.Counter()
    paths: collections.Counter = collections.Counter()
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        repo = d.get("repo", f.parts[-3])
        kept = len(d.get("findings") or [])
        dropped = int(d.get("dropped_by_category") or 0)
        total_kept += kept
        total_dropped += dropped
        print(f"{repo:36s} kept={kept:3d} dropped_by_category={dropped:4d} "
              f"categories={d.get('categories')}")
        for x in d.get("findings") or []:
            rules[f"{x.get('rule_id', '?')}"] += 1
            paths[f"{x.get('path', '?')}:{x.get('line', 0)}"] += 1
    print(f"\ntotal: kept={total_kept} dropped_by_category={total_dropped}")
    if rules:
        print("--- top rules ---")
        for k, v in rules.most_common(10):
            print(f"  {v:3d}  {k}")
    if paths:
        print("--- top locations ---")
        for k, v in paths.most_common(10):
            print(f"  {v:3d}  {k}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
