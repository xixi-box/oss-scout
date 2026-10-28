"""大范围扫描：从仓库池里找真正可认领的 bug issue。

    python scripts/wide_hunt.py [--pool FILE] [--window-lo H] [--window-hi H]
                               [--max-repos N] [--budget N]

策略（应对配额）：
- 用 `/repos/{o}/{r}/issues` 的 core 配额（5000/h），不用 search（30/min）。
- 每个仓库一次请求取最近 30 个 issue，本地过滤：有 bug 标签、开放、无 assignee。
- 只对通过初筛的少数候选查 timeline（确认没有 open 关联 PR）。
- 全程不写任何东西。

判据对应 AGENTS.md §0.2：assignee 为空、timeline 无 open cross-referenced PR、
评论少、正文足够长（能定位根因）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sh(args: list[str], timeout: int = 60) -> tuple[int, str]:
    p = subprocess.run(args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    return p.returncode, (p.stdout or "")


def load_pool(path: Path) -> list[str]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def open_xref(repo: str, number: int) -> int:
    """返回仍开放的关联 PR 数。取数失败返回 -1（调用方按有竞争处理）。"""
    rc, out = sh(["gh", "api", f"repos/{repo}/issues/{number}/timeline?per_page=100",
                  "--jq",
                  '[.[] | select(.event=="cross-referenced")'
                  ' | select((.source.issue.state // "open") == "open")] | length'])
    if rc != 0:
        return -1
    try:
        return int((out or "0").strip() or 0)
    except ValueError:
        return -1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", default=str(ROOT / "configs" / "scan_pool_wide.txt"))
    ap.add_argument("--window-lo", type=float, default=2)
    ap.add_argument("--window-hi", type=float, default=240)
    ap.add_argument("--max-repos", type=int, default=0)
    ap.add_argument("--budget", type=int, default=3000,
                    help="core API 调用上限（含初筛与 timeline）")
    ap.add_argument("--min-body", type=int, default=500)
    ap.add_argument("--out", default=str(ROOT / "state" / "wide_hunt.json"))
    args = ap.parse_args()

    repos = load_pool(Path(args.pool))
    if args.max_repos:
        repos = repos[:args.max_repos]
    now = dt.datetime.now(dt.timezone.utc)
    lo_t = now - dt.timedelta(hours=args.window_hi)
    hi_t = now - dt.timedelta(hours=args.window_lo)

    used = 0
    hits: list[dict] = []
    stats = {"repos": 0, "issues": 0, "passed_filter": 0, "skipped_quota": 0}

    for i, repo in enumerate(repos, 1):
        if used >= args.budget:
            print(f"!! budget {args.budget} reached; stopping at {repo}", file=sys.stderr)
            break
        rc, out = sh(["gh", "api",
                      f"repos/{repo}/issues?state=open&sort=created&direction=desc"
                      f"&per_page=30&labels=bug"])
        used += 1
        if rc != 0:
            print(f"[{i}/{len(repos)}] {repo}: fetch failed", file=sys.stderr)
            continue
        try:
            items = json.loads(out)
        except json.JSONDecodeError:
            continue
        stats["repos"] += 1

        for it in items:
            if it.get("pull_request"):
                continue
            if it.get("assignees"):
                continue
            labels = [str(l.get("name", "")).lower() for l in it.get("labels", [])]
            if not any("bug" in l for l in labels):
                continue
            # 维护者明确搁置 / 不算 bug 的标签一律跳过
            if any(k in " ".join(labels) for k in
                   ("parked", "duplicate", "wontfix", "invalid", "stale",
                    "needs more info", "needs-more-info", "question")):
                continue
            created = it.get("created_at", "")
            try:
                c = dt.datetime.fromisoformat(created.replace("Z", "+00:00"))
            except ValueError:
                continue
            if not (lo_t <= c <= hi_t):
                continue
            if it.get("comments", 0) > 4:
                continue
            body = it.get("body") or ""
            if len(body) < args.min_body:
                continue
            stats["issues"] += 1

            if used >= args.budget:
                stats["skipped_quota"] += 1
                continue
            x = open_xref(repo, it["number"])
            used += 1
            if x != 0:
                continue
            stats["passed_filter"] += 1
            hits.append({
                "repo": repo, "number": it["number"],
                "title": (it.get("title") or "")[:90],
                "created": created[:16],
                "age_h": round((now - c).total_seconds() / 3600, 1),
                "comments": it.get("comments", 0),
                "labels": [l.get("name") for l in it.get("labels", [])],
                "body_len": len(body),
                "url": it.get("html_url", ""),
            })
            print(f"  + {repo}#{it['number']}  {hits[-1]['title'][:60]}", flush=True)

        if i % 10 == 0:
            print(f"[{i}/{len(repos)}] used={used} hits={len(hits)}", file=sys.stderr)

    Path(args.out).write_text(json.dumps({"stats": stats, "hits": hits},
                                         ensure_ascii=False, indent=1),
                              encoding="utf-8")
    print(f"\n=== {len(hits)} candidates / {stats['repos']} repos / "
          f"{used} api calls ===", file=sys.stderr)
    for h in sorted(hits, key=lambda x: x["age_h"]):
        print("%-40s #%-6d %5.1fh c=%-2d %s" %
              (h["repo"], h["number"], h["age_h"], h["comments"], h["title"][:56]))
    print(f"\nsaved: {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
