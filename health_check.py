#!/usr/bin/env python3
"""oss-scout 健康周检：重算每个监控仓库的外部 PR 生态健康度 + CONTRIBUTING 政策闸，
写 state/health.json。每日任务的 agent 在 health.json 超过 7 天时自动重跑本脚本。

健康口径（与 2026-09-16 定标一致）：
  external_merges_75d  近 75 天被合并的外部贡献者 PR 数（排除 bot 与 MEMBER/OWNER/COLLABORATOR）
  median_days / le7d_pct  外部 PR 创建→合并 中位天数 / 7 天内合并占比
  zombie_ratio         创建超过 75 天仍 open 的 PR 数 ÷ external_merges_75d（腐烂率）
  policy               CONTRIBUTING 扫描出的政策闸（assignment_gate / bot_close / ai_disclaimer / no_ai_walls）
  lane_advisory        依据健康+政策给出的车道建议（消费者 agent 必须遵守）
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import re
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

_SRC = Path(__file__).resolve().parent
# 输出/配置根：容器里运行在 /data（卷持久化），本地开发在源码目录
import os as _os
ROOT = Path(_os.environ.get("OSSCOUT_HEALTH_ROOT") or
            (_SRC if (_SRC / "config.json").exists() else "/data"))
CONFIG_PATH = (_SRC / "config.json") if (_SRC / "config.json").exists() else (ROOT / "config.json")
CONFIG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
WINDOW_DAYS = 75

RE_ASSIGN_GATE = re.compile(
    r"(wait\s+for\s+assignment|must\s+be\s+assigned|assigned\s+to\s+you|"
    r"only.{0,40}reviewed.{0,40}when|approved\s+for\s+community|unassigned\s+PRs?)", re.I)
# 只有 PR 语境下的自动关闭才算闸门（issue 表单/stalebot 的自动关闭不算）
RE_BOT_CLOSE = re.compile(
    r"(?:(?:PR|pull\s+request)[^.]*(?:automatically|auto)[- ]clos|"
    r"(?:automatically|auto)[- ]clos[^.]*(?:PR|pull\s+request))", re.I)
RE_AI_DISCLAIMER = re.compile(r"(AI-?generated|AI\s+assistant|AI\s+tools)", re.I)
RE_NO_AI_WALLS = re.compile(r"(No\s+AI|walls?\s+of\s+text|not\s+acceptable\s+and\s+may\s+be\s+ignored)", re.I)


def gh(args: list[str]):
    p = subprocess.run(["gh", "api"] + args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if p.returncode == 0:
        return json.loads(p.stdout)
    if "secondary rate limit" in p.stderr.lower():
        time.sleep(25)
        return gh(args)
    return None


def search_page(q: str, page: int = 1, per_page: int = 100) -> dict:
    """search/issues + URL 查询串（gh 2.98 的 -f/-F 在部分端点会进请求体导致 404/422）。"""
    eq = urllib.parse.quote(q, safe="")
    return gh([f"search/issues?q={eq}&per_page={per_page}&page={page}"]) or {}


def external_merge_stats(repo: str) -> dict:
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=WINDOW_DAYS)).date().isoformat()
    q = f"repo:{repo} is:pr is:merged merged:>={since}"
    days, page = [], 1
    while page <= 10:  # 1000 条封顶，足够估中位数
        items = search_page(q, page).get("items", [])
        for it in items:
            who = (it.get("user") or {}).get("login", "")
            if who.endswith("[bot]") or it.get("author_association") in ("MEMBER", "OWNER", "COLLABORATOR"):
                continue
            fmt = lambda s: dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
            days.append((fmt(it["closed_at"]) - fmt(it["created_at"])).days)
        if len(items) < 100:
            break
        page += 1
        time.sleep(0.6)
    time.sleep(0.6)
    zombies = search_page(f"repo:{repo} is:pr is:open created:<={since}", 1, 1)
    open_issues = search_page(f"repo:{repo} is:issue is:open", 1, 1)
    time.sleep(0.6)
    n = len(days)
    return {
        "external_merges_75d": n,
        "median_days": (sorted(days)[n // 2] if n else None),
        "le7d_pct": (round(sum(1 for d in days if d <= 7) / n * 100) if n else None),
        "zombie_prs": zombies.get("total_count", 0),
        "zombie_ratio": (round(zombies.get("total_count", 0) / n, 2) if n else None),
        "open_issues": open_issues.get("total_count", 0),
    }


def policy_scan(repo: str) -> dict:
    content = ""
    for path in ("CONTRIBUTING.md", ".github/CONTRIBUTING.md", "docs/CONTRIBUTING.md"):
        res = gh([f"repos/{repo}/contents/{path}"])
        time.sleep(0.8)
        if res and res.get("content"):
            content = base64.b64decode(res["content"]).decode("utf-8", errors="replace")
            break
    if not content:
        return {"flags": ["no_contributing"], "verify_manually": True}
    flags = []
    if RE_ASSIGN_GATE.search(content):
        flags.append("assignment_gate")
    if RE_BOT_CLOSE.search(content):
        flags.append("bot_close")
    if RE_AI_DISCLAIMER.search(content):
        flags.append("ai_disclosure")
    if RE_NO_AI_WALLS.search(content):
        flags.append("no_ai_walls")
    return {"flags": flags or ["none"], "verify_manually": False}


def lane_advisory(stats: dict, policy: dict, config_lane: str) -> str:
    if "assignment_gate" in policy["flags"]:
        return "gated"
    if config_lane == "restricted":
        return "restricted"
    if stats["external_merges_75d"] < 5:
        return "observe"
    zr = stats["zombie_ratio"]
    if zr is not None and zr > 1.0:
        return "restricted"
    return config_lane


def main():
    repos: list[tuple[str, str]] = ([(r["repo"], "open") for r in CONFIG["open_pool"]]
                                    + [(r["repo"], "gated") for r in CONFIG["gated_pool"]]
                                    + [(r["repo"], "restricted") for r in CONFIG["restricted_pool"]]
                                    + [(r, "observe") for r in CONFIG["observe_only"]])
    out = {"computed_at": dt.datetime.now().isoformat(timespec="seconds"), "window_days": WINDOW_DAYS, "repos": {}}
    for repo, lane in repos:
        print(f"== {repo}")
        stats = external_merge_stats(repo)
        policy = policy_scan(repo)
        advisory = lane_advisory(stats, policy, lane)
        out["repos"][repo] = {"config_lane": lane, "stats": stats, "policy": policy, "lane_advisory": advisory}
        print(f"   {stats}  policy={policy['flags']}  lane→{advisory}")
        time.sleep(0.5)

    dest = ROOT / "state" / "health.json"
    tmp = dest.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    import os
    os.replace(tmp, dest)
    print(f"\n已写入 {dest}")


if __name__ == "__main__":
    main()
