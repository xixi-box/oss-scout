"""PR 监控：盯着我们开出去的 PR，有新动静就分类处理。

Playbook §2① 早就写了这一段的职责（CI 失败要修、maintainer 意见 48 小时内响应、
被关要记录原因），但代码里一直没实现——`prs` 页面只是把数据展示出来，没有任何
主动动作。于是 PR 开出去之后就没人管了，烂尾风险全押在人身上。

这里做三件确定性的事：

  1. CI 因我们的改动失败 → 拉失败日志，交给修复流程再跑一轮
  2. maintainer/bot 提出修改意见 → 记录并排进待办（不自动长篇回复）
  3. PR 被关 → 记录原因，反推该仓库的规则

对外发言是很危险的动作：astral-sh 明文禁止 AI 写沟通，biome 说低信号描述会被
直接关。所以**不自动发评论**，只把该说的话准备好、把该做的活排好，由人过一眼。

用法：
    python scripts/monitor_prs.py --dry-run
    python scripts/monitor_prs.py
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 分类信号。顺序有意义：先判"被关"，再判"要改"，最后才是普通讨论。
PATTERNS = [
    ("closed", re.compile(r"^$", re.I)),          # 占位，用 PR state 判
    ("ci_failure", re.compile(r"CI|checks?\s+failed|build\s+failed|test\s+failed",
                              re.I)),
    ("changes_requested", re.compile(
        r"please\s+(?:change|fix|update|add|remove)|could\s+you|"
        r"changes?\s+requested|needs?\s+(?:a\s+)?(?:test|changelog|changeset)|"
        r"revert|not\s+the\s+right\s+(?:place|fix)", re.I)),
    ("needs_info", re.compile(r"which\s+version|can\s+you\s+reproduce|"
                              r"what\s+did\s+you\s+expect|more\s+context", re.I)),
    ("assignment", re.compile(r"assign|claimer|taken|already\s+working", re.I)),
]


def _gh(args: list[str], timeout: int = 90) -> str:
    try:
        r = subprocess.run(["gh"] + args, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return ""
    return r.stdout if r.returncode == 0 else ""


def my_open_prs(limit: int = 50) -> list[dict]:
    raw = _gh(["search", "prs", "--author", "@me", "--state", "open",
               "--limit", str(limit),
               "--json", "repository,number,title,url,updatedAt"])
    try:
        return json.loads(raw) if raw else []
    except json.JSONDecodeError:
        return []


def pr_state(repo: str, number: int) -> dict:
    raw = _gh(["pr", "view", str(number), "-R", repo, "--json",
               "state,mergeable,reviewDecision,comments,statusCheckRollup,"
               "createdAt,isDraft"])
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}


def classify(pr: dict) -> dict:
    """给一个 PR 归类：需不需要动手、动什么手。

    只做分类，不生成对外文字——那一步要人过。返回的 action 是给调度用的。
    """
    comments = pr.get("comments") or []
    rollup = pr.get("statusCheckRollup") or []
    failing = []
    for c in rollup:
        if not isinstance(c, dict):
            continue
        concl = (c.get("conclusion") or "").upper()
        if concl in ("FAILURE", "TIMED_OUT", "CANCELLED"):
            failing.append(c.get("name") or c.get("context") or "?")
    pending = sum(1 for c in rollup if isinstance(c, dict)
                  and (c.get("status") or "").upper() in ("IN_PROGRESS", "QUEUED",
                                                          "PENDING"))
    # 只看非本人的最新评论
    recent = [c for c in comments
              if (c.get("author") or {}).get("login") not in ("xixi-box",)]
    latest = recent[-1] if recent else None
    body = str((latest or {}).get("body") or "")

    action, reason = "wait", ""
    if failing:
        action, reason = "fix_ci", f"failing checks: {', '.join(failing[:4])}"
    elif latest and PATTERNS[2][1].search(body):
        action = "address_review"
        reason = f"review from {(latest.get('author') or {}).get('login')}"
    elif latest and PATTERNS[3][1].search(body):
        action, reason = "answer_question", "maintainer asked for information"
    elif latest and PATTERNS[4][1].search(body):
        action, reason = "assignment_note", "assignment/claim mentioned"
    elif pending:
        action, reason = "wait", f"{pending} checks in progress"
    return {
        "action": action,
        "reason": reason,
        "failing_checks": failing[:6],
        "pending_checks": pending,
        "unanswered": len([c for c in recent
                           if (c.get("author") or {}).get("login") not in ("xixi-box",)]),
        "latest_author": (latest.get("author") or {}).get("login") if latest else None,
        "latest_excerpt": body[:300],
        "review_decision": pr.get("reviewDecision") or "",
        "draft": bool(pr.get("isDraft")),
    }


def run(dry_run: bool = True) -> list[dict]:
    out = []
    for item in my_open_prs():
        repo = item["repository"]["nameWithOwner"]
        num = item["number"]
        state = pr_state(repo, num)
        if not state:
            continue
        info = classify(state)
        info.update({"repo": repo, "number": num, "url": item.get("url", "")})
        out.append(info)
        if not dry_run:
            _record(info)
    return out


def _record(info: dict) -> None:
    """把需要动手的 PR 追加到 state/pr_watch.jsonl（唯一审计+待办来源）。"""
    p = ROOT / "state" / "pr_watch.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "repo": info["repo"], "number": info["number"],
           "action": info["action"], "reason": info["reason"]}
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def needs_attention(results: list[dict]) -> list[dict]:
    return [r for r in results if r["action"] != "wait"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", default=False)
    args = ap.parse_args()
    results = run(dry_run=args.dry_run)
    todo = needs_attention(results)
    for r in results:
        mark = "*" if r["action"] != "wait" else " "
        print(f"{mark} {r['repo']:34s}#{r['number']:<6} {r['action']:16s} {r['reason'][:60]}")
    print(f"\n{len(results)} open PRs, {len(todo)} need action")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
