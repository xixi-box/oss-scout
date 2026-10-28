"""批量筛真实候选：无 assignee、无关联 PR、评论少、根因描述清晰。

    python scripts/find_clean_issue.py <owner/repo> [n]

只做筛选与打印，不做任何写操作。判据对应 AGENTS.md §0.2 的复检项：
- issue open 且无 assignee
- timeline 无 cross-referenced（没人已开 PR）
- 评论数少（没人正在讨论方案）
"""
from __future__ import annotations

import json
import subprocess
import sys


def sh(args: list[str]) -> tuple[int, str]:
    p = subprocess.run(args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=120)
    return p.returncode, p.stdout


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: find_clean_issue.py <owner/repo> [limit]")
        return 2
    repo = sys.argv[1]
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 15

    rc, out = sh(["gh", "search", "issues", "--repo", repo, "--state", "open",
                  "--label", "bug", "--limit", str(limit),
                  "--json", "number,title,createdAt,commentsCount,assignees"])
    if rc != 0:
        print("search failed:", out[:200])
        return 1
    items = json.loads(out or "[]")

    clean = []
    for it in items:
        if it.get("assignees"):
            continue
        num = it["number"]
        rc2, tl = sh(["gh", "api", f"repos/{repo}/issues/{num}/timeline",
                      "--jq", '[.[] | select(.event=="cross-referenced")] | length'])
        xref = int((tl or "0").strip() or 0) if rc2 == 0 else -1
        if xref != 0:
            continue
        if it.get("commentsCount", 0) > 3:
            continue
        clean.append((num, it.get("commentsCount", 0), xref,
                      it.get("createdAt", "")[:10], it.get("title", "")[:72]))

    print(f"{repo}: {len(items)} bug issues scanned, {len(clean)} clean\n")
    for num, c, x, d, t in clean:
        print("#%-6d c=%-2d xref=%d %s  %s" % (num, c, x, d, t))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
