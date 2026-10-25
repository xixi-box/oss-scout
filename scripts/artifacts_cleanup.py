#!/usr/bin/env python
"""GitHub Actions artifact 存储配额盘点/清理（只操作 xixi-box 自己拥有的仓库）。

默认只盘点（--inventory）；--delete 需显式 --yes。删除策略：
  - GitHub 标记 expired 的 artifact 一律可删；
  - 或 --older-than-days N 以内的全部 artifact。
用法：
  python scripts/artifacts_cleanup.py --inventory
  python scripts/artifacts_cleanup.py --delete --older-than-days 30 --yes
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")


def gh(args: list[str], tries: int = 4) -> str | None:
    for i in range(tries):
        p = subprocess.run(["gh", "api"] + args, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if p.returncode == 0:
            return p.stdout
        time.sleep(min(30, (i + 1) * 8))
    print(f"    [gh fail] {args[:2]}: {p.stderr.strip()[-100:]}", file=sys.stderr)
    return None


def owned_repos() -> list[str]:
    out = gh(["user/repos", "-X", "GET", "-f", "per_page=100", "-f", "type=owner",
              "--jq", ".[].full_name"]) or "[]"
    return [s.strip() for s in out.strip().splitlines() if s.strip()]


def inventory() -> tuple[dict, int]:
    repos = owned_repos()
    print(f"own repos: {len(repos)}")
    report: dict = {}
    total_bytes = 0
    for repo in repos:
        out = gh([f"repos/{repo}/actions/artifacts", "-X", "GET", "-f", "per_page=100"])
        if out is None:
            continue
        arts = json.loads(out).get("artifacts", [])
        if not arts:
            continue
        entries = []
        for a in arts:
            entries.append({"id": a["id"], "name": a["name"][:50],
                            "size_mb": round(a["size_in_bytes"] / 1e6, 2),
                            "expired": a.get("expired", False),
                            "created": a.get("created_at", "")[:10]})
            total_bytes += a["size_in_bytes"]
        report[repo] = entries
        mb = sum(e["size_mb"] for e in entries)
        print(f"  {repo}: {len(entries)} artifacts, {mb:.1f} MB")
    print(f"TOTAL: {total_bytes/1e6:.1f} MB used by artifacts")
    return report, total_bytes


def delete(report: dict, older_than_days: int, include_expired: bool = True) -> int:
    cutoff = dt.date.today() - dt.timedelta(days=older_than_days)
    deleted = 0
    for repo, arts in report.items():
        for a in arts:
            created = dt.date.fromisoformat(a["created"]) if a["created"] else None
            expired = a["expired"]
            should = (include_expired and expired) or (created is not None and created <= cutoff)
            if not should:
                continue
            out = gh([f"repos/{repo}/actions/artifacts/{a['id']}", "-X", "DELETE"])
            if out is not None:
                deleted += 1
                print(f"  deleted {repo}#{a['id']} {a['name']} ({a['size_mb']}MB, "
                      f"{'expired' if expired else a['created']})")
            time.sleep(0.5)
    print(f"deleted {deleted} artifacts")
    return deleted


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inventory", action="store_true")
    ap.add_argument("--delete", action="store_true")
    ap.add_argument("--older-than-days", type=int, default=30)
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()
    if args.inventory or not args.delete:
        report, total = inventory()
        (sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1])))
        p = __import__("pathlib").Path("state/artifacts_inventory.json")
        p.write_text(json.dumps({"at": dt.datetime.now().isoformat(timespec="seconds"),
                                 "total_mb": round(total / 1e6, 1), "repos": report},
                                ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"saved {p}")
        return 0
    if not args.yes:
        print("refusing to delete without --yes")
        return 2
    report, _ = inventory()
    delete(report, args.older_than_days)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
