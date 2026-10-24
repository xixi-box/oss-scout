"""GitHub 只读访问层：复用 discover.py 的 gh CLI 调用模式，本层**无任何写函数**。

认领前实时复检（Playbook §0.2）：issue 状态、assignees、新评论认领信号、关联 PR。
注意 Playbook 已实测本机 TLS 抖动会导致 gh 调用偶发失败：复检取不到完整信号时
返回 None（信号不完整 ⇒ 宁可漏一条也不带假"无竞争"结论入库）。
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any

RE_CLAIM = re.compile(
    r"\bi\s?(?:'ll|will|'m\s+(?:working|on\s+it)|'d\s+like\s+to\s+(?:work|take|fix))\b"
    r"|\bworking\s+on\s+(?:this|a\s+fix|it)\b"
    r"|\btaking\s+(?:this|it)\b|\bpick(?:ed|ing)?\s+(?:this|it)\s?up\b"
    r"|\bPR\s+(?:is\s+)?(?:open|coming|up)\b|\bopened?\s+a\s+(?:PR|pull\s+request)\b",
    re.I,
)
MAINTAINER_ASSOC = {"MEMBER", "OWNER", "COLLABORATOR"}


def gh_read(args: list[str], max_retries: int = 2) -> Any | None:
    """只读 gh api 调用。尊重 Retry-After 语义的退避；失败返回 None。"""
    cmd = ["gh", "api"] + args
    for attempt in range(max_retries + 1):
        p = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if p.returncode == 0:
            return json.loads(p.stdout)
        err = p.stderr.lower()
        if "secondary rate limit" in err:
            import time
            time.sleep(25 * (attempt + 1))
            continue
        if attempt < max_retries:
            import time
            time.sleep(3 * (attempt + 1))
    return None


@dataclass
class RecheckResult:
    complete: bool                      # 信号是否完整；False = 不可作为"无竞争"依据
    issue_open: bool = False
    unassigned: bool = False
    assigned_to_me: bool = False        # 闸门池：是否指派给了 xixi-box
    has_claim_signal: bool = False
    has_cross_ref_pr: bool = False
    open_pr_hits: list[str] = field(default_factory=list)  # 按根因关键词搜到的 open PR
    errors: list[str] = field(default_factory=list)


def recheck_candidate(repo: str, number: int, *, my_login: str = "xixi-box",
                      root_cause_keywords: list[str] | None = None) -> RecheckResult | None:
    """认领前复检。任何一次取数失败 → complete=False（调用方应跳过该候选）。"""
    issue = gh_read([f"repos/{repo}/issues/{number}"])
    comments = gh_read([f"repos/{repo}/issues/{number}/comments?per_page=30"])
    if issue is None or comments is None:
        errs = []
        if issue is None:
            errs.append("issue fetch failed")
        if comments is None:
            errs.append("comments fetch failed")
        return RecheckResult(complete=False, errors=errs)

    r = RecheckResult(complete=True)
    r.issue_open = issue.get("state") == "open"
    assignees = [a.get("login", "") for a in issue.get("assignees") or []]
    r.unassigned = not assignees
    r.assigned_to_me = my_login in assignees
    for c in comments:
        login = (c.get("user") or {}).get("login", "")
        if login.endswith("[bot]") or login.endswith("bot"):
            continue
        if RE_CLAIM.search(c.get("body") or ""):
            r.has_claim_signal = True
            break
    timeline = gh_read([f"repos/{repo}/issues/{number}/timeline?per_page=100"])
    if timeline is None:
        r.complete = False
        r.errors.append("timeline fetch failed")
    else:
        for ev in timeline:
            if isinstance(ev, dict) and ev.get("event") == "cross-referenced":
                src = (ev.get("source") or {}).get("issue") or {}
                if src.get("pull_request"):
                    r.has_cross_ref_pr = True
                    break
    for kw in root_cause_keywords or []:
        q = f"repo:{repo} is:pr is:open {kw}"
        res = gh_read([f"search/issues?q={q}&per_page=10"])
        if res is None:
            r.complete = False
            r.errors.append(f"pr search failed for {kw!r}")
            continue
        for it in res.get("items", []):
            r.open_pr_hits.append(f"{repo}#{it['number']}: {it.get('title', '')[:80]}")
    return r
