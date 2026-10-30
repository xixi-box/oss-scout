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
            if not isinstance(ev, dict) or ev.get("event") != "cross-referenced":
                continue
            src = (ev.get("source") or {}).get("issue") or {}
            pr = src.get("pull_request")
            if not pr:
                continue
            # 只看仍可能落地的 PR：已关闭/已合并的引用不算竞争。
            # timeline 里 PR 的 state 在 source.issue.state（不在 pull_request 内），
            # 2026-10-28 实测返回 {"state":"closed"}。
            # 自己关掉一个测试 PR 后，cross-referenced 事件仍在 timeline 里，
            # 不复检这个字段会让该 issue 被永久拒绝。
            state = str(src.get("state") or pr.get("state") or "").lower()
            merged = (src.get("merged_at") or pr.get("merged_at")
                      or pr.get("merged"))
            if state == "closed" or merged:
                continue
            r.has_cross_ref_pr = True
            break
    import urllib.parse as _up
    # 只看 open PR：已关闭/已合并的历史尝试不算竞争（与 cross-ref 判定一致）。
    #
    # 命中判定分两档，权衡的是两类错误：
    #   误判（假阳性）→ 白放弃一个可做的 issue，代价=错过一次机会；
    #   漏判（假阴性）→ 开了重复 PR，代价=账号声誉，比错过机会严重得多。
    # 所以对**标识符类**关键词（含 _ 或 .，几乎必然是模块/函数名）严格匹配标题；
    # 对普通词则要求标题里**至少两个**关键词同时出现，才算同一个问题。
    # （实测误判来源：mypy#22230 "Spurious note..." 撞上 "spurious blank lines"、
    #  "Fix unanalyzed __getattr__..."——只共享一个泛词。）
    kws = [k for k in (root_cause_keywords or []) if k]
    strong = [k for k in kws if "_" in k or "." in k]
    weak = [k for k in kws if k not in strong]

    def _relevant(title: str) -> bool:
        low = title.lower()
        if strong:
            return any(s.lower() in low for s in strong)
        if not weak:
            return False
        # 普通词里，"最有辨识度"的那个（最长）必须命中，且总数要过半。
        # 2026-10-30 实测假阳性：pulumi#25237 "Deduplicate policy violations..."
        # 撞上 PR "include policy violations in JSON preview output"——
        # 共享 "policy"/"violations" 两个泛词，但 "deduplicate" 才是关键，
        # 它不在那个 PR 标题里，所以不该判成同一个问题。
        must = max(weak, key=len).lower()
        if must not in low:
            return False
        hits = sum(1 for w in weak if w.lower() in low)
        need = max(2, (len(weak) + 1) // 2)
        return hits >= need if len(weak) > 1 else hits >= 1

    for kw in kws:
        q = f"repo:{repo} is:pr is:open {kw}"
        # 必须 URL 编码：关键词含空格时直接拼进 URL 会让 HTTP/2 请求被拆坏，
        # GitHub 返回 PROTOCOL_ERROR（2026-10-28 实测 5/5 失败）
        res = gh_read([f"search/issues?q={_up.quote(q, safe='')}&per_page=10"])
        if res is None:
            # 关键词搜索是辅助信号：失败记录但不判整体不可用
            # （rate limit 下 search API 比其他端点更容易被拒）
            r.errors.append(f"pr search failed for {kw!r} (non-fatal)")
            continue
        for it in res.get("items", []):
            title = str(it.get("title", ""))
            if not _relevant(title):
                continue
            r.open_pr_hits.append(f"{repo}#{it['number']}: {title[:80]}")
    if not kws:
        # 没有关键词就等于没检查竞争，而这件事最贵的失败是"开了个重复 PR"。
        # 至少用 issue 编号搜一次（不少 PR 标题会带 #N 或正文 Fixes #N）。
        q = f"repo:{repo} is:pr is:open {number}"
        res = gh_read([f"search/issues?q={_up.quote(q, safe='')}&per_page=10"])
        if res is not None:
            for it in res.get("items", []):
                r.open_pr_hits.append(
                    f"{repo}#{it['number']}: {it.get('title', '')[:80]}")
    return r
