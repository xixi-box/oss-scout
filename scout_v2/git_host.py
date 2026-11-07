"""GitHub 只读访问层：复用 discover.py 的 gh CLI 调用模式，本层**无任何写函数**。

认领前实时复检（Playbook §0.2）：issue 状态、assignees、正文/评论里的认领·派单·
禁 AI 信号、关联 PR（timeline 交叉引用 + PR 文本里明写的 #<候选号>）。
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
    r"|\bPR\s+(?:is\s+)?(?:open|coming|up)\b|\bopened?\s+a\s+(?:PR|pull\s+request)\b"
    # 派单："you can fix this" / "@groeneai you can handle it"——活被指给了
    # 别人（2026-11-05 clickhouse#126287 实录：成员评论派单，我们没拦住）
    r"|(?:\byou\b|@\w[\w-]*)\s+can\s+(?:fix|take|work\s+on|handle)\s+(?:this|it|the)\b"
    # 禁 AI 介入：维护者明说这活不想让 AI 干（同一 issue 正文写
    # "the fix maybe is not trivial to ask AI to do it"）
    r"|\bnot\s+trivial\s+to\s+ask\s+ai\b|\bdon'?t\s+ask\s+ai\b"
    r"|\bnot\s+for\s+ai\b|\bai-?free\b",
    re.I,
)
MAINTAINER_ASSOC = {"MEMBER", "OWNER", "COLLABORATOR"}
# 维护者问句式派单（加权用，仅对 MEMBER/OWNER/COLLABORATOR 生效）：
# "Can you fix this?" / "Could you take it?" 出自维护者是任务转交，
# 出自普通用户更像求助。口径与 discover.py 的 maintainer_confirm 一致。
_RE_DISPATCH_MENTOR = re.compile(
    r"\b(?:can|could|would)\s+you\s+(?:fix|take|work\s+on|handle)\s+(?:this|it|the)\b",
    re.I,
)

# "duplicate of #123" / "dupe of #123" / "same as #123" / "dup: #123"
# 只认明确指向另一条 issue 的措辞。两个排除：
#   - 否定句 "not a duplicate of #123"、"no longer a dupe of #123"
#   - 句号/换行之后的新句子（避免跨句误配）
_RE_DUPLICATE_OF = re.compile(
    r"\b(?:duplicate|dupe|dup|same)\s*(?:of|as|:)?\s*#(\d{1,7})\b", re.I)
_RE_DUPLICATE_NEGATED = re.compile(
    r"\b(?:not|never|no\s+longer|isn'?t|wasn'?t)\s+(?:a\s+)?"
    r"(?:duplicate|dupe|dup)\s*(?:of|as|:)?\s*#\d{1,7}", re.I)


def _is_bot_login(login: str) -> bool:
    return login.endswith("[bot]") or login.endswith("bot")


def _explicit_issue_ref(text: str, number: int) -> bool:
    """文本里是否明写 "#<候选号>"（"Refs #123"/"Fixes #123"）。
    带词边界：候选 #7883 不会误配 #78830 / #17883。"""
    if not text or number <= 0:
        return False
    return re.search(rf"#{number}\b", text) is not None


def gh_read(args: list[str], max_retries: int = 2) -> Any | None:
    """只读 gh api 调用。尊重 Retry-After 语义的退避；失败返回 None。"""
    cmd = ["gh", "api"] + args
    for attempt in range(max_retries + 1):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=45)
        except subprocess.TimeoutExpired:
            return None
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


def gh_pages(path: str, size: int) -> list | None:
    """A failed later page is incomplete evidence, never an empty comment section."""
    result = []
    page = 1
    while True:
        suffix = "" if page == 1 else f"&page={page}"
        batch = gh_read([f"{path}?per_page={size}{suffix}"])
        if not isinstance(batch, list):
            return None
        result.extend(batch)
        if len(batch) < size:
            return result
        page += 1


@dataclass
class RecheckResult:
    complete: bool                      # 信号是否完整；False = 不可作为"无竞争"依据
    issue_open: bool = False
    unassigned: bool = False
    assigned_to_me: bool = False        # 闸门池：是否指派给了 xixi-box
    has_claim_signal: bool = False
    has_cross_ref_pr: bool = False
    open_pr_hits: list[str] = field(default_factory=list)  # 按根因关键词搜到的 open PR
    # 2026-11-03 实录：qdrant#11361 修对了 bug，但关联的 #11360 已被标成 #10465
    # 的 duplicate，真正的修复战场在 #10465（已有 #10470 在推进）。PR 开出几小时
    # 就被 cloud bot 关掉。把"被判重复"和"指向的主 issue"显式记下来。
    is_duplicate: bool = False
    duplicate_of: int | None = None     # 主 issue 编号（正文 "duplicate of #N"）
    errors: list[str] = field(default_factory=list)
    issue_title: str | None = None
    issue_body: str = ""
    screening_source: tuple[dict, list, list] | None = field(default=None, repr=False)


def linked_fix_prs(timeline: list, number: int, my_login: str = "xixi-box") -> list[dict]:
    """Return active cross-references and merged PRs explicitly referencing this issue."""
    linked = []
    for event in timeline:
        if not isinstance(event, dict) or event.get("event") != "cross-referenced":
            continue
        src = (event.get("source") or {}).get("issue") or {}
        pr = src.get("pull_request")
        if not pr:
            continue
        author = str((src.get("user") or {}).get("login") or "")
        if author.lower() == my_login.lower():
            continue
        state = str(src.get("state") or pr.get("state") or "").lower()
        merged = src.get("merged_at") or pr.get("merged_at") or pr.get("merged")
        text = f"{src.get('title') or ''}\n{src.get('body') or ''}"
        if (state != "closed" and not merged) or (merged and _explicit_issue_ref(text, number)):
            linked.append({"url": src.get("html_url") or pr.get("html_url"), "author": author,
                           "state": "merged" if merged else state, "title": src.get("title")})
    return linked


def recheck_candidate(repo: str, number: int, *, my_login: str = "xixi-box",
                      root_cause_keywords: list[str] | None = None) -> RecheckResult | None:
    """认领前复检。任何一次取数失败 → complete=False（调用方应跳过该候选）。"""
    issue = gh_read([f"repos/{repo}/issues/{number}"])
    comments = gh_pages(f"repos/{repo}/issues/{number}/comments", 30)
    if issue is None or comments is None:
        errs = []
        if issue is None:
            errs.append("issue fetch failed")
        if comments is None:
            errs.append("comments fetch failed")
        return RecheckResult(complete=False, errors=errs)

    r = RecheckResult(complete=True, issue_title=issue.get("title") or "",
                      issue_body=issue.get("body") or "")
    r.issue_open = issue.get("state") == "open"
    assignees = [a.get("login", "") for a in issue.get("assignees") or []]
    r.unassigned = not assignees
    r.assigned_to_me = my_login in assignees
    # 被判重复：state_reason 是权威信号；正文里的 "duplicate of #N" 给出主 issue。
    # 注意这条与"是否 open"独立——GitHub 上可以先标 duplicate 后关闭，也可能
    # 保持 open 一段时间，我们正好在那个窗口里撞进去过（qdrant#11360）。
    if str(issue.get("state_reason") or "").lower() == "duplicate":
        r.is_duplicate = True
    body_text = str(issue.get("body") or "")
    m_dup = _RE_DUPLICATE_OF.search(body_text)
    # 否定句不算："This is not a duplicate of #123" 是开发者在排除重复。
    # 有否定就整条不认，比逐句匹配更保守——宁可漏一个 duplicate，也不要把
    # 正常的 issue 当成重复跳过。
    if m_dup and not _RE_DUPLICATE_NEGATED.search(body_text):
        r.is_duplicate = True
        try:
            r.duplicate_of = int(m_dup.group(1))
        except (TypeError, ValueError):
            r.duplicate_of = None
    # 认领/派单信号：issue 正文 + 评论都扫。2026-11-05 实录
    # clickhouse/clickhouse#126287：成员 PedroTadim 在 issue **正文**里写
    # "the fix maybe is not trivial to ask AI to do it"，随后又在评论里派单
    # "you can fix this"——旧逻辑只扫评论，两层信号全漏，认领并开了 PR。
    # bot 作者的文本跳过（官方 bot 的套话不是认领），与原评论循环口径一致。
    signals: list[tuple[str, str]] = []  # (text, author_association)
    if not _is_bot_login(str(((issue.get("user") or {}).get("login")) or "")):
        signals.append((body_text, str(issue.get("author_association") or "")))
    for c in comments:
        if _is_bot_login(str(((c.get("user") or {}).get("login")) or "")):
            continue
        signals.append((str(c.get("body") or ""),
                        str(c.get("author_association") or "")))
    for text, assoc in signals:
        if RE_CLAIM.search(text):
            r.has_claim_signal = True
            break
        # 加权：问句式派单出自 MEMBER/OWNER/COLLABORATOR 是任务转交
        # （"Can you fix this?"），出自普通用户更像求助，所以只对维护者
        # 生效——author_association 口径与 discover.py 的 maintainer_confirm
        # 相同。
        if assoc in MAINTAINER_ASSOC and _RE_DISPATCH_MENTOR.search(text):
            r.has_claim_signal = True
            break
    timeline = gh_pages(f"repos/{repo}/issues/{number}/timeline", 100)
    if timeline is None:
        r.complete = False
        r.errors.append("timeline fetch failed")
    else:
        r.has_cross_ref_pr = bool(linked_fix_prs(timeline, number, my_login))
        r.screening_source = (issue, comments, timeline)
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
            # 排除我们自己开的 PR：修复成功开出的 PR 会被关键词搜索命中，
            # 不排除就会在下一轮复检里被判成"有人抢先"（2026-11-02 实测：
            # 今天 5 个 PR 对应的候选全被标成 aborted_competed，同时额度被
            # abort 路径归还，于是同一天连开 5 个）。
            author = str(((it.get("user") or {}).get("login")) or "")
            if author and author.lower() == my_login.lower():
                continue
            title = str(it.get("title", ""))
            # 文本级直接关联兜底（deer-flow#7883 实录：竞争 PR body 写
            # "Refs #7883"，标题与根因关键词无交集，只按 _relevant 判会漏；
            # timeline 单页 per_page=100 且按最旧在前返回，活跃 issue 的最新
            # cross-referenced 事件也可能根本不在返回里）。搜索响应必带全量
            # body：PR 标题/正文明写 "#<候选号>" 即判竞争，不再要求标题命中。
            if not (_relevant(title) or _explicit_issue_ref(
                    f"{title}\n{it.get('body') or ''}", number)):
                continue
            r.open_pr_hits.append(f"{repo}#{it['number']}: {title[:80]}")
    if not kws:
        # 没有关键词就等于没检查竞争，而这件事最贵的失败是"开了个重复 PR"。
        # 至少用 issue 编号搜一次（不少 PR 标题会带 #N 或正文 Fixes #N）。
        q = f"repo:{repo} is:pr is:open {number}"
        res = gh_read([f"search/issues?q={_up.quote(q, safe='')}&per_page=10"])
        if res is not None:
            for it in res.get("items", []):
                author = str(((it.get("user") or {}).get("login")) or "")
                if author and author.lower() == my_login.lower():
                    continue
                r.open_pr_hits.append(
                    f"{repo}#{it['number']}: {it.get('title', '')[:80]}")
    return r
