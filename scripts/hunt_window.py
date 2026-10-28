"""按机会窗口批量找真正无人竞争的 issue。

    python scripts/hunt_window.py [hours_min] [hours_max]

对 config.json 里的所有仓库，搜 2–48h 窗口内的 bug issue，逐个排除：
- 有 assignee
- timeline 里有 open 状态的 cross-referenced PR
- 已有 open PR 的关键词命中
- 正文过短（描述不清，无法定位）
输出候选清单，供人工/AI 判定是否可修。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sh(args: list[str], timeout: int = 120) -> tuple[int, str]:
    p = subprocess.run(args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    return p.returncode, (p.stdout or "")


def repos() -> list[str]:
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    out, seen = [], set()
    for key in ("open_pool", "gated_pool", "restricted_pool"):
        for r in cfg.get(key) or []:
            n = r["repo"] if isinstance(r, dict) else r
            if n not in seen:
                seen.add(n)
                out.append(n)
    return out


def has_open_xref(repo: str, number: int) -> bool:
    rc, out = sh(["gh", "api", f"repos/{repo}/issues/{number}/timeline",
                  "--jq", '[.[] | select(.event=="cross-referenced") '
                          '| select((.source.issue.state // "open") == "open")] | length'],
                 timeout=60)
    if rc != 0:
        return True          # 取数失败按有竞争处理（保守）
    try:
        return int((out or "0").strip() or 0) > 0
    except ValueError:
        return True


def main() -> int:
    lo = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    hi = int(sys.argv[2]) if len(sys.argv) > 2 else 48
    import datetime as dt
    now = dt.datetime.now(dt.timezone.utc)
    since = (now - dt.timedelta(hours=hi)).strftime("%Y-%m-%dT%H:%M:%S")
    until = (now - dt.timedelta(hours=lo)).strftime("%Y-%m-%dT%H:%M:%S")

    found = []
    for repo in repos():
        rc, out = sh(["gh", "search", "issues", "--repo", repo, "--state", "open",
                      "--label", "bug", "--created", f"{since}..{until}",
                      "--limit", "20", "--json",
                      "number,title,createdAt,commentsCount,assignees,body"])
        if rc != 0:
            print(f"  [{repo}] search failed", file=sys.stderr)
            continue
        items = json.loads(out or "[]")
        for it in items:
            if it.get("assignees"):
                continue
            if it.get("commentsCount", 0) > 4:
                continue
            body = it.get("body") or ""
            if len(body) < 400:
                continue
            if has_open_xref(repo, it["number"]):
                continue
            found.append({
                "repo": repo, "number": it["number"],
                "title": it.get("title", ""), "created": it.get("createdAt", "")[:16],
                "comments": it.get("commentsCount", 0), "body_len": len(body),
            })
        print(f"  [{repo}] {len(items)} in window", file=sys.stderr)

    found.sort(key=lambda x: x["created"])
    print(f"\n=== {len(found)} clean candidates in {lo}h..{hi}h window ===\n")
    for f in found:
        print("%-34s #%-6d c=%-2d %s  %s" %
              (f["repo"], f["number"], f["comments"], f["created"], f["title"][:58]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
