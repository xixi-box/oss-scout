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
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = Path(os.environ.get("OSSCOUT_DASHBOARD_ROOT", str(ROOT)))

from scout_v2.roster import monitored_repo
from scout_v2.contribution_intent import (claim_evidence, coordination_evidence,
                                         pr_automation_hold, MULTILINGUAL_REVIEW_RULES)
from scout_v2.state_store import STAGED_FULL_REASON, atomic_json_write, queue_transaction

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

# 纯"催 review / 打招呼"的评论：没有任何可执行诉求。为这类评论起草技术回复
# 是自说自话——对方只是想知道什么时候有人看，AI 编一段"我改了 X"反而露怯。
# 判定方式是词袋而非句式：先剔掉 @提及（@org/team 里的斜杠会把正则打断），
# 再看有没有催办词、有没有真正的诉求词。命中催办词且无诉求词才算客套。
_RE_MENTION = re.compile(r"@[\w./-]+")
_RE_PING = re.compile(
    r"\b(?:all\s+green|ci\s+is\s+green|no\s+conflicts?|rebased|bump|ping|"
    r"kindly\s+review|could\s+someone|take\s+a\s+look|when\s+you\s+get\s+a\s+chance|"
    r"any\s+update|still\s+waiting|lgtm|looks\s+good)\b", re.I)
# 只要出现这些词就说明对方真提了事，必须按 review 处理，宁可多判一次。
# 不收名词性的 "update"/"change"——"any update on this?" 是催办，不是诉求。
_RE_ACTIONABLE = re.compile(
    r"\b(?:please|should|need\s+to|must|why|how|instead|rename|revert|"
    r"missing|wrong|breaks?|fails?|failing|error|bug|tests?|"
    r"add\s+(?:a\s+)?(?:test|check|log|assert)|remove\s+this|"
    r"fix\s+this|change\s+this)\b", re.I)
# "why/how" 只是提问，落到 review 分类还得有 review 的句式兜住。
_RE_REVIEWISH = re.compile(
    r"\b(?:why|how\s+(?:does|do|is|are)|instead|should|please|could\s+you)\b",
    re.I)


def is_pleasantry(body: str) -> bool:
    """判断一条评论是不是纯客套/催 review（规则兜底用）。

    只在模型不可用时使用。两道条件同时成立才算：出现催办词，且没有任何
    诉求词。反过来漏判一次只是多起草一封草稿（不影响正确性），误判成
    客套会真的漏掉维护者意见，所以诉求词表取得宽。
    """
    text = _RE_MENTION.sub(" ", str(body or "")).strip()
    if not text or len(text) > 400:
        return False
    return bool(_RE_PING.search(text)) and not _RE_ACTIONABLE.search(text)


CLASSIFY_PROMPT = """A maintainer or bot just commented on our pull request.
Decide what the contributor behind this PR should do next.

Classify into exactly one:
  wait              -- nothing to do. Review pings ("all green, could someone
                       review?"), bot notices, approvals, thanks, or comments
                       that only restate status. Answering these adds noise.
  address_review    -- they asked for a concrete change (code, tests,
                       changelog, naming, revert).
  answer_question   -- they asked something only a human can answer, or
                       reported a problem they need explained.
  assignment_note   -- the comment is about who is assigned / whether the PR is
                       still wanted.

Judge intent, not keywords. "All green here, but why does this swallow the
exception?" is answer_question, not wait. "Thanks!" alone is wait.
Read Chinese, English and mixed-language comments with equal care, including negation and chronology.
"为避免两边重复投入，我想继续跟进 #4167" and "I submitted my own fix in #4167"
are assignment_note (ownership/duplicate-work coordination), not wait or a code-change request.
Chinese requests to add a regression test are address_review; questions are answer_question.
Do not discard an unfamiliar language as having no actionable intent.

Pull request: {repo}#{number}
Title: {title}

Comment from {author}:
{body}

Answer with exactly two lines:
ACTION: <one of the four words>
REASON: <max 15 words, what the comment actually asks>"""


def _model() -> tuple[str, str]:
    """(api_key, model) 用于评论分类。缺失时返回空串让调用方走规则兜底。"""
    try:
        from scout_v2.workers.issue_scorer import load_scorer_config
        cfg = load_scorer_config(DATA_ROOT)
        return str(cfg.get("api_key") or ""), str(cfg.get("model")
                                                      or "deepseek-v4.1-flash")
    except Exception:  # noqa: BLE001
        return "", ""


# 评论正文的传输上限。取大值而非"够用就行"：截断一次就可能漏掉一条要求，
# 2026-11-04 实录（docker/compose#14450）——维护者 740 字的评审被 [:500]
# 切掉后半段，起草出的回复漏了"加 Matches 级回归测试"这条明确要求。
# 这个值只防病态输入（粘贴整份日志），正常 review 一律完整传递。
COMMENT_TEXT_LIMIT = 8000

# 评论分类的合法取值。模型输出必须落在其中，否则当作解析失败走规则兜底。
_ACTIONS = ("wait", "address_review", "answer_question", "assignment_note")

# 纯流程性 bot：它们的发言是模板通知，没有对话价值（changeset 提醒、
# 覆盖率报告、artifacts 清理）。注意不包括"已被人认领"这类——那要留痕。
_NOISE_BOT_RE = re.compile(
    r"changeset-bot|dependabot|renovate|codecov|github-actions|"
    r"pre-commit-ci|netlify|vercel|sonarcloud|stale|sre-ci-robot", re.I)
# sre-ci-robot 的 ciloop-result 是 CI 结果通报（过/不过都有）：通过无需动作，
# 不过会体现在 statusCheckRollup 里走 fix_ci——通报本身永远不该触发 review 修复
# （milvus#55034 实录：CI 全绿的通报派了一轮 opencode 空跑）。


def _is_noise_bot(author: str, body: str) -> bool:
    who = str(author or "")
    # sre-ci-robot 是普通账号不是 GitHub App（无 [bot] 后缀），单列
    if not (who.endswith("[bot]") or who.lower() == "sre-ci-robot"):
        return False
    if not _NOISE_BOT_RE.search(who):
        return False
    # bot 发的是"认领/冲突"信息时仍然要留痕，交给后面的规则判
    return not (PATTERNS[4][1].search(body or "") or claim_evidence(body) or coordination_evidence(body))


def is_human_followup_author(author: str, author_type: str | None = None) -> bool:
    login = author.lower()
    return bool(login and login not in (*MY_LOGINS, "sre-ci-robot")
                and not login.endswith("[bot]") and author_type != "Bot")

# 分类结果缓存：key = "repo#num@created_at"。
# 监控每 15 分钟扫一遍所有 open PR，没有缓存就是每轮重判同一批评论。
# 只看最新一条评论，所以缓存键带上它的时间戳就够——新评论自然是新键。
CLASSIFY_CACHE = Path(os.environ.get("OSSCOUT_DASHBOARD_ROOT", str(ROOT))) \
    / "state" / "pr_classify_cache.json"
_CACHE_MAX = 500


def _cache_load() -> dict:
    try:
        return json.loads(CLASSIFY_CACHE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _cache_put(key: str, verdict: dict) -> None:
    if not key:
        return
    try:
        data = _cache_load()
        data[key] = verdict
        if len(data) > _CACHE_MAX:      # 只留最近的，文件不能无限长
            for k in list(data)[:len(data) - _CACHE_MAX]:
                data.pop(k, None)
        CLASSIFY_CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CLASSIFY_CACHE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, CLASSIFY_CACHE)
    except OSError:
        pass


def classify_comment(body: str, *, repo: str = "", number: int = 0,
                     title: str = "", author: str = "") -> dict:
    """用模型判断一条评论的意图。返回 {"ok", "action", "reason"}。

    ok=False 表示模型不可用/输出不合法——调用方必须回退规则判定，
    绝不能让"模型挂了"变成"这条评论不用管"。
    """
    text = str(body or "").strip()
    if not text:
        return {"ok": True, "action": "wait", "reason": "no comment"}
    if coordination_evidence(text):
        return {"ok": True, "action": "assignment_note", "reason": "ownership / duplicate-work coordination"}
    text = text[:COMMENT_TEXT_LIMIT]
    key, model = _model()
    if not key:
        return {"ok": False, "action": "wait", "reason": "no model key"}
    try:
        from scout_v2.console_models import opencode_base_url
        from scout_v2.workers.issue_scorer import GoClient
        client = GoClient(key=key, model=model, timeout=60, base_url=opencode_base_url(DATA_ROOT))
        ok, detail = client.chat(CLASSIFY_PROMPT.format(
            repo=repo or "?", number=number, title=title or "?",
            author=author or "?", body=text), max_tokens=120)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "action": "wait", "reason": f"model error: {e}"[:120]}
    if not ok:
        return {"ok": False, "action": "wait", "reason": f"model error: {detail}"[:120]}

    m_action = re.search(r"ACTION:\s*([a-z_]+)", detail, re.I)
    m_reason = re.search(r"REASON:\s*(.+)", detail, re.I)
    action = (m_action.group(1).lower() if m_action else "")
    if action not in _ACTIONS:
        return {"ok": False, "action": "wait",
                "reason": f"unparseable model output: {detail[:80]}"}
    reason = (m_reason.group(1).strip() if m_reason else "")[:160]
    return {"ok": True, "action": action, "reason": reason}


def _classify_by_rules(body: str, latest: dict) -> tuple[str, str]:
    """模型不可用时的保守兜底：能判的都判，判不了就当 review 处理。

    方向选得明确——多起草一封草稿只是白花一次模型调用，漏掉维护者的
    修改意见会让 PR 烂尾。
    """
    author = (latest.get("author") or {}).get("login")
    if coordination_evidence(body) or claim_evidence(body):
        return "assignment_note", "ownership / duplicate-work coordination (rule fallback)"
    if is_pleasantry(body) or re.fullmatch(
            r"\s*(?:thanks?(?:\s+you)?|thank\s+you|ok(?:ay)?|done|谢谢(?:你|您|了)?|感谢|收到|好的)[.!！。\s]*", body, re.I):
        return "wait", "pleasantry / review ping (rule fallback)"
    if PATTERNS[2][1].search(body) or re.search(r"(?:请|建议|需要|应该|麻烦)[^。！？\n]{0,30}(?:增加|添加|补充|修改|删除|移除|重命名|修复|测试)", body):
        return "address_review", f"review from {author} (rule fallback)"
    if _RE_REVIEWISH.search(body):
        return "answer_question", "question about the change (rule fallback)"
    if PATTERNS[3][1].search(body):
        return "answer_question", "maintainer asked for information (rule fallback)"
    if PATTERNS[4][1].search(body):
        return "assignment_note", "assignment/claim mentioned (rule fallback)"
    if not body.strip() or re.fullmatch(r"[0-9a-fA-F]{7,40}", body.strip()):
        return "wait", "empty comment or commit reference (rule fallback)"
    return "answer_question", "unclassified comment requires semantic follow-up (rule fallback)"


def _gh(args: list[str], timeout: int = 90) -> str:
    try:
        r = subprocess.run(["gh"] + args, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return ""
    return r.stdout if r.returncode == 0 else ""


def run_gh(args: list[str], timeout: int = 90) -> int:
    """跑 gh 命令只关心成败（workflow_dispatch 成功时 stdout 为空，_gh 区分不了）。"""
    try:
        r = subprocess.run(["gh"] + args, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return r.returncode
    except (subprocess.TimeoutExpired, OSError):
        return 124


def my_open_prs(limit: int = 50) -> list[dict]:
    raw = _gh(["search", "prs", "--author", "@me", "--state", "open",
               "--limit", str(limit),
               "--json", "repository,number,title,url,updatedAt"])
    try:
        return [pr for pr in json.loads(raw or "[]")
                if monitored_repo(pr["repository"]["nameWithOwner"])]
    except json.JSONDecodeError:
        return []


def pr_state(repo: str, number: int) -> dict:
    raw = _gh(["pr", "view", str(number), "-R", repo, "--json",
               "state,mergeable,reviewDecision,comments,statusCheckRollup,"
               "createdAt,isDraft,title,body,url"])
    try:
        state = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}
    # gh pr view 的 comments 只有 issue 层。把行内 review 评论合进来
    # （合成 author/body/createdAt 形态），分类器才能看见维护者写在
    # 代码旁边的修改要求（agentgateway#4264 实录）。
    inline_raw = _gh(["api", f"repos/{repo}/pulls/{number}/comments?per_page=100",
                      "--jq", r'.[] | "\(.created_at)|\(.user.login)|\(.body)"'])
    inline = []
    for line in (inline_raw or "").splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3:
            inline.append({"author": {"login": parts[1]},
                           "body": parts[2], "createdAt": parts[0]})
    inline.sort(key=lambda c: c["createdAt"])
    state["comments"] = sorted(
        (state.get("comments") or []) + inline,
        key=lambda c: c.get("createdAt") or "")
    return state


def classify(pr: dict) -> dict:
    """给一个 PR 归类：需不需要动手、动什么手。

    只做分类，不生成对外文字——那一步要人过。返回的 action 是给调度用的。
    """
    from scout_v2.console_models import pr_comment_trigger_enabled
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
    latest_author = str(((latest or {}).get("author") or {}).get("login") or "")
    if failing:
        # CI 失败是确定性信号，不需要模型判断
        action, reason = "fix_ci", f"failing checks: {', '.join(failing[:4])}"
    elif latest and not pr_comment_trigger_enabled(DATA_ROOT):
        action, reason = "wait", "PR comment trigger disabled"
    elif latest and _is_noise_bot(latest_author, body):
        # 机器人通知（changeset/dependabot/artifacts）按定义没有对话价值，
        # 也省一次模型调用。真正的 CI 结论走上面的 statusCheckRollup；
        # "已被他人认领"这类 bot 消息要留痕，不能在这里吞掉。
        action, reason = "wait", f"bot notice from {latest_author}"
    elif latest:
        # 评论性质由模型判断，规则只做兜底。
        # 2026-11-04 教训：「All green, could someone review?」和
        # 「All green, but the test fails」对人是一眼的事，对正则只差一个词。
        # 缓存键含评论时间戳：同一轮 15 分钟扫描之间不重复调用模型。
        ckey = (f"multilingual-v1:{pr.get('repo') or pr.get('pr_url') or ''}#"
                f"{pr.get('number') or ''}@{latest.get('createdAt') or ''}")
        cached = _cache_load().get(ckey) if latest.get("createdAt") else None
        if cached:
            action, reason = cached["action"], cached["reason"] + " (cached)"
        else:
            ai = classify_comment(body, repo=str(pr.get("repo") or ""),
                                  number=int(pr.get("number") or 0),
                                  title=str(pr.get("title") or ""),
                                  author=str((latest.get("author") or {})
                                             .get("login") or ""))
            action, reason = ai["action"], ai["reason"]
            if not ai["ok"]:
                # 模型不可用 → 退回规则判定（保守方向：宁可多判 review）
                action, reason = _classify_by_rules(body, latest)
            elif latest.get("createdAt"):
                _cache_put(ckey, {"action": action, "reason": reason})
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
        "latest_excerpt": body[:300],          # 一行提示用；全文见 latest_body
        "latest_body": body[:COMMENT_TEXT_LIMIT],
        "pr_title": str(pr.get("title") or ""),
        "pr_url": str(pr.get("url") or ""),
        "pr_body": str(pr.get("body") or "")[:2000],
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


# ---- L1/L2：CI 失败的自动修复派发 ----

# 这些失败模式与我们的改动无关，修代码没有意义，派发前先排除。
EXTERNAL_CHECK_PATTERNS = re.compile(
    r"Vercel|pre-run-check|Package Label|check-issue-link|DCO|"
    "cla|CLA|EasyCLA|authorize|authorization", re.I)
# 日志里的确凿外因信号。FLAKY 优先级最高——它们自带 FAILED 字样，
# 必须先于 _looks_like_test_failure 判断，否则实时 LLM 测试的
# "FAILED ... test_exploration[gpt-live]" 会被当成我们的测试失败（2026-11-01）。
FLAKY_LOG_PATTERNS = re.compile(
    r"gpt-live|test_exploration", re.I)
EXTERNAL_LOG_PATTERNS = re.compile(
    r"Vercel - |Authorization required|"
    "must have the .verified.|DO NOT request for the label|"
    "awaiting maintainer assignment|not assigned to the linked issue", re.I)

# 去 key：每个 (repo, pr, head_sha) 只派一次——修完 head 会变，新 sha 才允许再试。
ATTEMPTS_FILE = DATA_ROOT / "state" / "pr_fix_attempted.json"


def pr_head(repo: str, number: int) -> dict:
    raw = _gh(["pr", "view", str(number), "-R", repo, "--json",
               "headRefName,headRefOid,headRepository,headRepositoryOwner,isCrossRepository,state,author"])
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}


def failing_details(repo: str, number: int) -> list[dict]:
    """每个失败 check 的名字与日志尾部（拿得到才带 excerpt）。

    gh pr view 的 statusCheckRollup 只给结论不给日志；日志要逐个 run 去拉，
    成本高，所以这里只对"名字像我们工作流产生的问题"的检查拉日志尾部，
    其余只记名字——外因分类主要靠名字就够。
    """
    raw = _gh(["pr", "view", str(number), "-R", repo, "--json", "statusCheckRollup"])
    try:
        rollup = json.loads(raw).get("statusCheckRollup") or []
    except (json.JSONDecodeError, AttributeError):
        return []
    out = []
    for c in rollup:
        if not isinstance(c, dict):
            continue
        concl = (c.get("conclusion") or "").upper()
        if concl not in ("FAILURE", "TIMED_OUT", "ACTION_REQUIRED"):
            continue
        name = c.get("name") or c.get("context") or "?"
        excerpt = ""
        details = c.get("detailsUrl") or ""
        # 对 check runs（有 checkSuites url）才尝试拉日志摘要；status context
        # （jenkins/DCO 这类）没有可拉的日志
        run_url = c.get("detailsUrl") or ""
        m = re.search(r"/runs/(\d+)", run_url)
        if m and c.get("__typename") == "CheckRun":
            log = _gh(["run", "view", m.group(1), "-R", repo, "--log-failed"], timeout=120)
            excerpt = (log or "")[-2500:]
        out.append({"name": name, "conclusion": concl,
                    "details": details[:200], "excerpt": excerpt})
    return out


def failure_is_ours(failing: list[dict]) -> tuple[bool, str]:
    """(是否值得派修复, 原因)。外因/权限类返回 False。"""
    if not failing:
        return False, "no failing checks"
    joined = " ".join(
        f.get("name", "") + " " + f.get("excerpt", "") for f in failing)
    # 1) flaky 优先：实时 LLM 测试的日志长什么样都带 FAILED，先认标记
    if FLAKY_LOG_PATTERNS.search(joined):
        return False, "flaky live-LLM test (not our change)"
    # 2) 名字层外因：全是权限/指派类检查且日志里没有真实测试失败痕迹
    names = " | ".join(f.get("name", "") for f in failing)
    if EXTERNAL_CHECK_PATTERNS.search(names):
        logs = " ".join(f.get("excerpt", "") for f in failing)
        if not logs or (EXTERNAL_LOG_PATTERNS.search(logs)
                        and not _looks_like_test_failure(logs)):
            return False, "external check by name (no real test failure)"
    # 3) 日志层外因
    if EXTERNAL_LOG_PATTERNS.search(joined) and not _looks_like_test_failure(joined):
        return False, "external/permission failure (log match)"
    return True, f"{len(failing)} failing check(s)"


def _looks_like_test_failure(text: str) -> bool:
    """log 里有没有真实的测试/编译失败痕迹（区别于纯权限/标签提示）。

    刻意不含 "exit code N"：每个 CI 步骤结束都有 exit code，pydantic-ai 的
    Package Label 检查日志尾部就带 "exit code 1"，把它算进去会让外因检查
    形同虚设（2026-11-02 实录）。"error:" 同理太宽，rust 用 error[ 精确匹配。
    """
    return bool(re.search(
        r"FAILED |AssertionError|Traceback \(most recent|error\[|"
        "compilation error|panic:", text))


def _attempts() -> dict:
    try:
        return json.loads(ATTEMPTS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def dispatch_pr_fix(repo: str, number: int, **kwargs) -> dict:
    if pr_automation_hold(DATA_ROOT, repo, number):
        return {"dispatched": False, "reason": "automatic PR actions paused by user"}
    from scout_v2.console_models import pr_comment_trigger_enabled
    if kwargs.get("kind", "ci") in ("followup", "review") and not pr_comment_trigger_enabled(DATA_ROOT):
        return {"dispatched": False, "reason": "PR comment trigger disabled"}
    if not monitored_repo(repo):
        return {"dispatched": False, "reason": "repository outside monitored roster"}
    import sqlite3
    ATTEMPTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(ATTEMPTS_FILE.with_suffix(".sqlite3"))
    try:
        db.execute("BEGIN IMMEDIATE")
        return _dispatch_pr_fix(repo, number, **kwargs)
    finally:
        db.close()


def _dispatch_pr_fix(repo: str, number: int, *, dry_run: bool = False,
                    kind: str = "ci", review_comment: str = "",
                    review_author: str = "", force: bool = False,
                    comment_id: int | None = None, source_kind: str | None = None) -> dict:
    """对挂红的 PR（或需按 review 改的 PR）派一轮 opencode 修复。

    kind="ci"       CI 挂红，修到绿
    kind="review"   维护者提了具体修改要求，落实要求

    两者走同一条待审批流水线。CI 按 head_sha 去重，评论跟进按来源和评论 ID 去重。

    返回 {dispatched, reason, ...}。外因不派、非我们的 fork 不派、已修过不派。
    """
    head = pr_head(repo, number)
    if not head:
        return {"dispatched": False, "reason": "pr head unavailable"}
    if (head.get("state") != "OPEN" or (head.get("author") or {}).get("login") != "xixi-box"
            or not head.get("isCrossRepository") or
            (head.get("headRepositoryOwner") or {}).get("login") != "xixi-box"):
        return {"dispatched": False, "reason": "head not on our fork"}

    head_sha = head.get("headRefOid", "")
    review_source = None
    review_id = None
    attempts = _attempts()
    if kind == "followup":
        from datetime import datetime, timedelta, timezone
        thread = _thread(repo, number, limit=None)
        human = [c for c in thread if c["body"].strip()
                 and is_human_followup_author(c["author"], c.get("author_type"))]
        if comment_id is not None:
            human = [c for c in human if c["comment_id"] == comment_id and c["source"] == source_kind]
        else:
            since = (datetime.now(timezone.utc) - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")
            human = [c for c in human if c["created_at"] >= since]
        prefix = f"{repo.lower()}#{number}@followup:"
        for candidate in human:
            group = candidate.get("review_id")
            if group and not any(c["source"] == "review" and c["comment_id"] == group
                                 and c.get("review_state") not in ("PENDING", "DISMISSED") for c in thread):
                continue
            candidate_key = prefix + (f"review:{group}" if group else
                                       f"{candidate['source']}:{candidate['comment_id']}")
            if candidate_key in attempts and not force and attempts[candidate_key].get("reason") != "dispatch failed":
                continue
            if any(c["author"] in MY_LOGINS and c["created_at"] > candidate["created_at"]
                   and ((c["source"] == "inline" and c["thread_id"] == candidate["thread_id"])
                        if candidate["source"] == "inline"
                        else comment_id is None and c["source"] == "issue")
                   for c in thread):
                continue
            review_source, review_id, key = candidate, group, candidate_key
            break
        if not review_source:
            return {"dispatched": False, "reason": "no actionable human comment"}
    elif kind == "review":
        # 指令文本 = 全部对方评论（含行内），不是只有最新一条——
        # 2026-11-05 实录：yhmo 三条意见同时在场，"取最新一条"让 fix
        # 指令变成了机器人的 CI 通报，两条真要求被挤丢了。
        merged = _thread(repo, number)
        ext = [c for c in merged if c.get("author") == review_author
               and c.get("association") in ("OWNER", "MEMBER", "COLLABORATOR")
               and c.get("body", "").strip() == review_comment.strip()[:COMMENT_TEXT_LIMIT]]
        if not ext:
            return {"dispatched": False, "reason": "review author or comment not verified"}
        review_source = ext[-1]
        review_comment = review_source["body"]
        # review 类去重不能用 head_sha：同一条意见在推 commit 之前会被反复看到
        digest = hashlib.sha1(
            f"{repo}#{number}:{review_source['source']}:{review_source['comment_id']}:{review_comment}".encode()).hexdigest()[:12]
        key = f"{repo}#{number}@review:{digest}"
    else:
        key = f"{repo}#{number}@{head_sha[:12]}"
    if key in attempts and not force and attempts[key].get("reason") != "dispatch failed":
        return {"dispatched": False, "reason": f"already attempted: {key}"}

    if kind == "ci":
        failing = failing_details(repo, number)
        ours, why = failure_is_ours(failing)
        if not ours:
            attempts[key] = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                             "dispatched": False, "reason": why}
            _save_attempts(attempts)
            return {"dispatched": False, "reason": why}
    elif kind == "review":
        if not review_comment.strip():
            return {"dispatched": False, "reason": "empty review comment"}
        failing = []
        why = f"review request from {review_author or 'maintainer'}"
    else:
        failing = []
        why = f"comment from {review_source['author']}"

    if dry_run:
        return {"dispatched": False, "reason": f"dry-run: would fix ({why})"}

    # bot 评论一起带给 agent：changeset 缺失这类机械要求就是修复项（L2）
    raw = _gh(["api", f"repos/{repo}/issues/{number}/comments", "--jq",
               r'.[] | select(.body | test("changeset|changelog|CLAUDE|coderabbit|'
               r'sonarqube|lint|format"; "i")) | "\(.user.login)\t\(.body)"']) if kind == "ci" else ""
    comments = []
    for line in (raw or "").splitlines()[:8]:
        parts = line.split("\t", 1)
        if len(parts) == 2:
            comments.append({"author": parts[0], "body": parts[1][:600]})

    fork_name = (head.get("headRepository") or {}).get("name")
    if not fork_name:
        return {"dispatched": False, "reason": "fork name unavailable"}
    payload = {"fork_repo": f"xixi-box/{fork_name}",
               "branch": head.get("headRefName", ""),
               "upstream_repo": repo, "pr_number": number,
               "head_sha": head_sha, "kind": kind,
               "failing": failing[:6], "comments": comments,
               "review_comment": review_comment[:4000] if kind == "review" else "",
               "review_author": review_author if kind == "review" else "",
               "review_comment_id": review_source["comment_id"] if review_source else None,
               "review_id": review_id,
               "source_kind": review_source["source"] if review_source else None}
    from scout_v2.console_models import repair_settings
    payload["repair_agent"], payload["model"] = repair_settings(DATA_ROOT)
    import base64 as _b64
    fix_b64 = _b64.b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode()
    job_id = "prfix-" + hashlib.sha256(key.encode()).hexdigest()[:32]
    attempts[key] = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": kind,
                     "dispatched": False, "reason": "dispatch pending confirmation"}
    _save_attempts(attempts)
    # _gh 失败返回 ""，但 workflow run 成功时 stdout 也是空——用 run_gh 拿 rc
    rc = run_gh(["workflow", "run", "oss-scout-pr-fix.yml", "-R", "xixi-box/oss-scout",
                 "-f", f"fix_b64={fix_b64}", "-f", f"job_id={job_id}"], timeout=60)
    ok = rc == 0
    attempts[key] = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                     "dispatched": ok,
                     "kind": kind,
                     "reason": why if ok else "dispatch failed"}
    _save_attempts(attempts)
    return {"dispatched": ok, "reason": why, "job_id": job_id, "kind": kind,
            "fork_repo": payload["fork_repo"],
            "branch": payload["branch"]}


def _save_attempts(data: dict) -> None:
    atomic_json_write(ATTEMPTS_FILE, data)


# ---- L3：回复草拟（只起草，绝不自动发）----

REPLIES_FILE = DATA_ROOT / "state" / "pr_replies.json"


def _thread(repo: str, number: int, limit: int | None = 10) -> list[dict]:
    """PR 上的全部人话：issue 层评论 + 行内 review 评论，按时间合并。

    2026-11-04 实录（agentgateway#4264）：维护者把修改要求写在行内评论里，
    而 issues/{n}/comments 根本不含行内评论——分类器和起草器都看不见，
    一条"必须改代码"的 review 被静默漏了两天。pulls/{n}/comments 才是
    行内评论的家。两个端点是不同的集合，不会重复。
    """
    out = []
    for source, endpoint in (
            ("issue", f"repos/{repo}/issues/{number}/comments"),
            ("inline", f"repos/{repo}/pulls/{number}/comments"),
            ("review", f"repos/{repo}/pulls/{number}/reviews")):
        raw = _gh(["api", f"{endpoint}?per_page=100", "--paginate", "--slurp"])
        if not raw and limit is None:
            raise RuntimeError(f"unable to read PR {source} discussion")
        for page in json.loads(raw or "[]"):
            for comment in page:
                if not (comment.get("created_at") or comment.get("submitted_at")):
                    continue
                body = comment.get("body") or ""
                out.append({"created_at": comment.get("created_at") or comment["submitted_at"],
                            "author": (comment.get("user") or {}).get("login", ""),
                            "author_type": (comment.get("user") or {}).get("type"),
                            "association": comment.get("author_association"),
                            "comment_id": comment["id"],
                            "body": body if limit is None else body[:COMMENT_TEXT_LIMIT],
                            "url": comment.get("html_url"),
                            "thread_id": comment.get("in_reply_to_id") or comment["id"],
                            "review_id": comment.get("pull_request_review_id") or
                                         (comment["id"] if source == "review" else None),
                            "path": comment.get("path"), "line": comment.get("line"),
                            "start_line": comment.get("start_line"),
                            "original_line": comment.get("original_line"),
                            "original_start_line": comment.get("original_start_line"),
                            "commit_id": comment.get("commit_id"),
                            "diff_hunk": comment.get("diff_hunk"), "review_state": comment.get("state"),
                            "source": source})
    out.sort(key=lambda c: c["created_at"])
    return out if limit is None else out[-limit:]


MY_LOGINS = ("xixi-box",)


def latest_external_comment(repo: str, number: int) -> dict | None:
    """对方（非我们）在这条 PR 上的最新发言。

    任何要展示"对方评论"的地方都必须走这里，不能用 comments[-1]：
    我们发出回复之后，最后一条就是我们自己的措辞，再把它标成
    "对方评论"就是张冠李戴（2026-11-04 实录）。
    """
    thread = _thread(repo, number, limit=30)
    for c in reversed(thread):
        if str(c.get("author") or "") not in MY_LOGINS:
            return c
    return None


def _root_cause(pr_body: str) -> str:
    """从 PR 描述里抽 Root cause 段（Playbook §5：根因只来自真实产出）。"""
    m = re.search(r"^#*\s*Root cause:?\s*$(.+?)(?=^#*\s*\w|\Z)",
                  pr_body or "", re.I | re.M | re.S)
    if not m:
        m = re.search(r"Root cause:\s*(.+?)(?:\n\n|\Z)", pr_body or "",
                      re.I | re.S)
    return re.sub(r"\s+", " ", m.group(1)).strip()[:300] if m else ""


def draft_reply(repo: str, number: int, *, force: bool = False,
                context: dict | None = None) -> dict:
    """AI 起草对最新未回评论的回复，存 state/pr_replies.json。

    只起草不发送：外发文字过审是人的事（Playbook §2①，astral/biome 前车之鉴）。
    同一 PR 已有未发送草稿且评论没更新时跳过（省一次模型调用）。

    context（可选）来自 classify() 的同一份 PR 数据，用于给飞书卡片补上
    issue 标题/根因/对方原话——免得为了发一张卡片再拉一遍 GitHub。
    """
    if pr_automation_hold(DATA_ROOT, repo, number):
        return {"drafted": False, "reason": "automatic PR actions paused by user"}
    from scout_v2.console_models import pr_comment_trigger_enabled
    if not pr_comment_trigger_enabled(DATA_ROOT):
        return {"drafted": False, "reason": "PR comment trigger disabled"}
    if not monitored_repo(repo):
        return {"drafted": False, "reason": "repository outside monitored roster"}
    try:
        replies = json.loads(REPLIES_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        replies = {}
    key = f"{repo}#{number}"
    thread = _thread(repo, number)
    # 回复的对象必须是**别人**的最新发言。我们发出回复后，thread[-1] 就是
    # 我们自己的话——不排除的话，下一轮会对着自己的回复再起草一遍，
    # 卡片上也会把我们的措辞标成对方评论（2026-11-04 实录）。
    external = [c for c in thread
                if str(c.get("author") or "") not in ("xixi-box",)]
    if not thread:
        return {"drafted": False, "reason": "no comments"}
    if not external:
        return {"drafted": False, "reason": "only our own comments"}
    latest = external[-1]
    expected = (context or {}).get("review_comment_id")
    if expected and latest.get("comment_id") != expected:
        return {"drafted": False, "reason": "review comment superseded"}
    prev = replies.get(key) or {}
    if (not force and prev.get("status") == "draft"
            and prev.get("for_comment") == latest.get("created_at")):
        return {"drafted": False, "reason": "draft already current",
                "key": key, "draft": prev.get("draft")}
    # 已回复过这条评论就不再起草：我们的最新发言比对方的最新发言晚，
    # 说明球在对面——再起草就是对同一条评论刷屏（2026-11-04 实录：
    # compose#14450 已发送后每 15 分钟被重新起草、重新推卡片）。
    ours = [c for c in thread if str(c.get("author") or "") in MY_LOGINS]
    if (not force and ours and latest
            and str(ours[-1].get("created_at") or "") > str(latest.get("created_at") or "")):
        return {"drafted": False, "reason": "already replied; waiting on them",
                "key": key}

    # 模型配置只从 _model() 走：分类和起草共用一处，测试也只 stub 一处。
    # 变量名绝不能叫 key——上面 key 是回复的落库键（repo#number），
    # 曾被这里覆盖成 API key，草稿全写到错键下、看板永远读不到。
    api_key, model = _model()
    if not api_key:
        return {"drafted": False, "reason": "no model key"}
    from scout_v2.console_models import opencode_base_url
    from scout_v2.workers.issue_scorer import GoClient

    convo = "\n".join(f"[{c['author']}] {c['body'][:COMMENT_TEXT_LIMIT]}"
                      for c in thread[-6:])
    prompt = (
        "You maintain an open-source contribution bot. A maintainer or bot replied "
        "on our pull request. Draft the reply a polite human contributor would "
        "write.\n"
        "Rules: max 6 lines; concrete and technical; no promises about timing; "
        "no AI disclaimers; if the comment asks for a code change, acknowledge and "
        "say what will change (the fix itself is handled separately); plain text, "
        "no markdown headers.\n\n"
        f"{MULTILINGUAL_REVIEW_RULES}\n\n"
        f"Conversation on {repo}#{number}:\n{convo}\n\n"
        f"Confirmed repair result: {json.dumps((context or {}).get('fix_result') or {})}\n"
        "Describe a change as completed only when the confirmed repair result says it was pushed.\n"
        "Reply with ONLY the reply text.")

    client = GoClient(key=api_key, model=model, timeout=180, base_url=opencode_base_url(DATA_ROOT))
    ok, detail = client.chat(prompt)
    if not ok:
        return {"drafted": False, "reason": f"model error: {detail[:120]}"}

    entry = {"status": "draft",
                    "for_comment": latest.get("created_at"),
                    "to_author": latest.get("author"),
                    "draft": detail.strip()[:1200],
                    "pr_title": str((context or {}).get("pr_title") or "")[:200],
                    "pr_url": str((context or {}).get("pr_url") or ""),
                    "root_cause": _root_cause(
                        str((context or {}).get("pr_body") or "")),
                    "source_comment": str(latest.get("body") or "")[:COMMENT_TEXT_LIMIT],
                    # 行内评论要回到行内线程回复（pulls comments/{id}/replies），
                    # 行外的进 issue 层。发送端按这两个字段选端点。
                    "source_kind": str(latest.get("source") or "issue"),
                    "source_comment_id": latest.get("comment_id"),
                    "drafted_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    with queue_transaction(REPLIES_FILE.parents[1], path=REPLIES_FILE) as current:
        previous = current.get(key) or {}
        if not force and previous.get("draft") and previous.get("for_comment") == entry["for_comment"]:
            return {"drafted": False, "reason": "draft already current"}
        current[key] = {**previous, **entry}
    return {"drafted": True, "key": key, "entry": current[key]}


def _record(info: dict) -> None:
    """把需要动手的 PR 追加到 state/pr_watch.jsonl（唯一审计+待办来源）。"""
    p = DATA_ROOT / "state" / "pr_watch.jsonl"
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


# ---- 飞书多维表格的数据源：所有 PR 一行一条 ----

def all_my_prs(limit: int = 1000) -> list[dict]:
    """我们开出去的全部 PR（open + closed + merged），搜索接口一次拿一类。

    closed 和 merged 共用 `--state closed`，结果的 state 字段能区分二者；
    所以只打两次搜索。搜索结果不带仓库全名时从 repository.nameWithOwner 取。
    """
    out: dict = {}
    for state in ("open", "closed"):
        raw = _gh(["search", "prs", "--author", "@me", "--state", state,
                   "--limit", str(limit),
                   "--json", "repository,number,title,state,url,createdAt,"
                             "updatedAt,closedAt"])
        try:
            if not raw:
                raise RuntimeError(f"PR {state} snapshot unavailable")
            items = json.loads(raw)
        except json.JSONDecodeError as ex:
            raise RuntimeError("invalid PR snapshot") from ex
        if not isinstance(items, list) or len(items) >= limit:
            raise RuntimeError("PR snapshot incomplete; refusing to prune ledger")
        for it in items:
            key = (it["repository"]["nameWithOwner"], str(it["number"]))
            out[key] = {
                "repo": it["repository"]["nameWithOwner"],
                "number": int(it["number"]),
                "title": it.get("title") or "",
                "state": str(it.get("state") or "").lower(),
                "url": it.get("url") or "",
                "created_at": it.get("createdAt") or "",
                "updated_at": it.get("updatedAt") or "",
                "closed_at": it.get("closedAt") or "",
            }
    return list(out.values())


# 台账口径（用户裁定 2026-11-04）：未合并就关掉的 PR 不进表；自己名下的
# 仓库（xixi-box/*，练习/测试/作业类）和 Deep-Optica 相关仓库不是开源贡献，
# 都剔除。merged 保留——被合并才是贡献的终点证明。
LEDGER_EXCLUDE_STATES = {"closed"}
# 我们自己的 GitHub 账号名下的仓库：xixi-box（机器人）和 wangshun-china（个人）
LEDGER_OWN_OWNERS = {"xixi-box", "wangshun-china"}


def ledger_excludes(row: dict) -> bool:
    if str(row.get("state")) in LEDGER_EXCLUDE_STATES:
        return True
    owner = str(row.get("repo", "")).split("/", 1)[0].lower()
    if owner in LEDGER_OWN_OWNERS:
        return True
    if "optica" in str(row.get("repo", "")).lower():
        return True
    return False


def build_pr_rows(root) -> list[dict]:
    """多维表格的行：GitHub 全量 PR ⊕ 本地动作/回复/修改状态。

    本地状态是增强，不是来源——GitHub 拉不到（如网络抖动）时行照样产出，
    只是增强列为空。
    """
    rows = all_my_prs()
    if not rows:
        return []

    seen = {}
    queue = {}
    for name, target in (("state/seen.json", seen), ("queue/candidates.json", queue)):
        try:
            target.update(json.loads((root / name).read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    issue_by_pr = {str(entry["pr_url"]).lower(): key for key, entry in queue.items()
                   if entry.get("pr_url")}
    db = root / "state/scout_v2.sqlite3"
    if db.is_file():
        conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            for repo, head, url in conn.execute("SELECT repo,head,url FROM publications WHERE day IS NOT NULL"):
                issue = re.fullmatch(r"[^:]+:fix/issue-(\d+)-[0-9a-f]+(?:-[0-9a-f]+)?", head)
                if issue and url:
                    issue_by_pr.setdefault(url.lower(), f"{repo}#{issue[1]}")
        except sqlite3.OperationalError as exc:
            if "no such table: publications" not in str(exc):
                raise
        finally:
            conn.close()
    discovered = {key.lower(): entry.get("discovered_at") for key, entry in queue.items()}
    discovered.update({key.lower(): entry["first_seen"] for key, entry in seen.items() if entry.get("first_seen")})

    # pr_watch.jsonl：每个 PR 最近一次动作
    watch: dict = {}
    try:
        for line in (root / "state" / "pr_watch.jsonl") \
                .read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = (str(rec.get("repo")), str(rec.get("number")))
            watch[key] = rec          # 后写覆盖先写
    except OSError:
        pass

    # review 派发时间（attempts 文件），用于"进行中"状态的对账
    attempts_review: dict = {}
    try:
        attempts_data = json.loads((root / "state" / "pr_fix_attempted.json")
                                   .read_text(encoding="utf-8"))
        for akey, av in attempts_data.items():
            if str(av.get("kind")) == "review":
                attempts_review[akey.split("@")[0]] = av
    except (OSError, json.JSONDecodeError):
        pass

    # pr_replies.json：回复草稿状态
    replies: dict = {}
    try:
        data = json.loads((root / "state" / "pr_replies.json")
                          .read_text(encoding="utf-8"))
        for key, entry in data.items():
            replies[key] = entry
    except (OSError, json.JSONDecodeError):
        pass

    for row in rows:
        pr_url = row.get("url") or f"https://github.com/{row['repo']}/pull/{row['number']}"
        row["discovered_at"] = discovered.get(issue_by_pr.get(pr_url.lower(), "").lower())
        key = (row["repo"], str(row["number"]))
        rec = watch.get(key) or {}
        row["action"] = str(rec.get("action") or "unknown")
        note_parts = [str(rec.get("reason") or "").strip()]

        entry = replies.get(f"{row['repo']}#{row['number']}") or {}
        rstatus = str(entry.get("status") or "")
        row["reply_status"] = {"draft": "待审", "sent": "已发送",
                               "dismissed": "已放弃"}.get(rstatus, "")
        fstate = str(entry.get("fix_state") or "")
        # 卡死在"进行中"的状态对账：review 派发超过 2 小时仍无回调，说明
        # 那一轮运行早已结束（成功推送或被验证闸拒绝）——回调时代（
        # fix-result 401）的历史遗留一律按已推送落账，真实依据以 PR 分支
        # 上的 commit 为准。
        if fstate == "pending":
            fdone = str(entry.get("fix_staged_at") or
                        entry.get("fix_done_at") or "")
            if not fdone:
                fdone = str((attempts_review.get(key) or {}).get("ts") or "")
            if fdone:
                try:
                    import datetime as _dt
                    age = (_dt.datetime.now() -
                           _dt.datetime.fromisoformat(fdone)).total_seconds()/3600
                    if age >= 2:
                        fstate = "unknown"
                except ValueError:
                    pass
        row["fix_state"] = {"pending": "进行中", "pushed": "已推送",
                            "failed": "失败", "unknown": "待核查"}.get(fstate, "")
        if entry.get("sent_body"):
            note_parts.append("已发回复：" + str(entry["sent_body"])[:120])
        if entry.get("fix_test_result"):
            note_parts.append(str(entry["fix_test_result"])[:120])
        row["note"] = "；".join(p for p in note_parts if p)[:600]

    return [r for r in rows if not ledger_excludes(r)]


def build_repo_rows(root) -> list[dict]:
    """「仓库池」表的行：监控名单 ⊕ 池审计（语言/stars/最近 push）。

    两个来源都是本地文件（configs/repositories.yaml + state/pool_audit.json），
    零 GitHub 调用——池子变化本来就低频，审计文件跑 audit_pool 时刷新。
    """
    import yaml
    cfg_path = root / "configs" / "repositories.yaml"
    if not cfg_path.exists():
        cfg_path = ROOT / "configs" / "repositories.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    repos = cfg.get("repositories") or {}

    audit: dict = {}
    for path in (root / "state" / "pool_audit.json",
                 ROOT / "state" / "pool_audit.json"):
        try:
            for item in json.loads(path.read_text(encoding="utf-8")) or []:
                audit[str(item.get("repo") or "")] = item
            break
        except (OSError, json.JSONDecodeError):
            continue

    rows = []
    for repo, spec in repos.items():
        spec = spec or {}
        if spec.get("internal"):
            continue
        a = audit.get(repo) or {}
        rows.append({
            "repo": repo,
            "lane": spec.get("lane") or "open",
            "internal": bool(spec.get("internal")),
            "pool": spec.get("pool") or "formal",
            "issue_scan": spec.get("issue_scan", True),
            "purpose": spec.get("purpose") or "",
            "merged_by_us": spec.get("merged_by_us", 0),
            "lang": a.get("lang") or "",
            "stars": a.get("stars"),
            "pushed": a.get("pushed") or "",
            "note": spec.get("note") or "；".join(str(x) for x in (a.get("gate_workflows") or []))[:400],
        })
    return rows


def build_scan_rows(root, running_keys=(), published=None, pr_states=None) -> list[dict]:
    """「扫描发现」表的行：seen.json 全量镜像。

    这是发现层的总账——Worker 扫到的每个 issue 都在，含被规则否决和
    低分淘汰的。排序按发现时间倒序，表里最新的在最上面（飞书默认视图
    按插入顺序，所以行的顺序不影响，但便于调试）。
    """
    seen_path = root / "state" / "seen.json"
    if not seen_path.exists():
        seen_path = ROOT / "state" / "seen.json"
    try:
        seen = json.loads(seen_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    queue = {}
    try:
        qpath = root / "queue" / "candidates.json"
        if not qpath.exists():
            qpath = ROOT / "queue" / "candidates.json"
        queue = json.loads(qpath.read_text(encoding="utf-8")) or {}
    except (OSError, json.JSONDecodeError):
        pass

    from scout_v2.feishu_bitable import _timestamp

    # Earlier competition aborts did not save the reason on the candidate.
    historical = {key for key, entry in queue.items()
                  if (entry or {}).get("status") == "aborted_competed"
                  and not (entry or {}).get("abort_reason")}
    historical_reasons = {}
    activity = {}
    actions = (
        "pipeline_coarse_screen", "pipeline_prestart_coarse_screen", "pipeline_fine_screen",
        "pipeline_abort", "pipeline_pr_open", "pipeline_reconciled", "pipeline_publication_status",
        "dashboard_pr_approval_ready", "dashboard_pr_approval_delivery_error",
        "dashboard_pr_followup_no_approval", "dashboard_pr_fix_result", "dashboard_pr_reply_sent",
        "dashboard_pr_reply_failed",
    )
    try:
        with (root / "state" / "worklog.jsonl").open(encoding="utf-8") as worklog:
            for line in worklog:
                if not any(action in line for action in actions):
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                key, reason = str(event.get("target") or ""), str(event.get("result") or "")
                action = event.get("action")
                if key in historical and action == "pipeline_abort" and reason.startswith("competed:"):
                    historical_reasons[key] = reason
                stamp = event.get("ts")
                if action in actions and (_timestamp(stamp) or 0) > (_timestamp(activity.get(key.lower())) or 0):
                    activity[key.lower()] = stamp
    except OSError:
        pass

    rows = []
    for key, v in seen.items():
        v = v or {}
        if "#" not in key:
            continue
        candidate = queue.get(key) or {}
        candidate_status = str(candidate.get("status") or "")
        queue_state = queue_display_state(candidate_status, candidate.get("updated_at"))
        pr_url = (published or {}).get(key.lower()) or (
            candidate.get("pr_url") if candidate_status in _QUEUE_GREEN else None
        )
        pr_key = re.fullmatch(r"https://github\.com/([^/]+/[^/]+)/pull/(\d+)", pr_url or "")
        last_activity = max((v.get("first_seen"), v.get("updated_at"), candidate.get("updated_at"),
                             candidate.get("started_at"), candidate.get("staged_at"), activity.get(key.lower()),
                             activity.get((pr_url or "").lower()),
                             activity.get(f"{pr_key[1]}#{pr_key[2]}".lower()) if pr_key else None),
                            key=lambda stamp: _timestamp(stamp) or 0)
        result = candidate_activity_state(key, candidate_status, running_keys)
        reason = candidate.get("abort_reason")
        coarse = candidate.get("coarse_reason")
        if not coarse and candidate_status in {
                "aborted_competed", "aborted_closed", "aborted_duplicate", "needs_cla",
                "needs_maintainer_confirm", "gated_alignment_only", "observe_only", "out_of_scope"}:
            coarse = reason or historical_reasons.get(key) or _SCAN_OTHER_OUTCOMES.get(candidate_status) or candidate_status
        if candidate_status == "aborted_stale" and candidate.get("blockers"):
            coarse = reason or ", ".join(candidate["blockers"])
        if not result and not pr_url and candidate_status != "screen_skipped":
            if coarse:
                result = f"粗筛淘汰 · {coarse}"
            elif candidate_status in {"fine_blocked", "aborted_stale"}:
                result = f"细筛淘汰 · {reason or candidate_status}"
            elif candidate_status in {"aborted_no_evidence", "aborted_pipeline"}:
                result = f"修复失败 · {reason or candidate_status}"
            elif candidate_status.startswith("aborted_"):
                result = f"开工流程拦截 · {reason or candidate_status}"
            elif candidate_status in _SCAN_OTHER_OUTCOMES:
                label = _SCAN_OTHER_OUTCOMES[candidate_status]
                result = f"{label} · {reason}" if reason else label
            elif candidate_status == "queue_pruned":
                result = "已移出候选队列 · 超出 10 条上限"
            elif v.get("status") == "archived":
                result = "已移出候选队列"
        rows.append({
            "key": key,
            "status": "screen_skipped" if candidate_status == "screen_skipped" else str(v.get("status") or ""),
            # 兼容旧记录：早期只写一个 why，按状态归到对应列
            "coarse_reason": str(coarse or v.get("coarse_reason")
                                 or (v.get("why") if str(v.get("status"))
                                     in ("blocked", "unknown_repo", "too_fresh")
                                     else "") or ""),
            "fine_reason": "未执行" if candidate_status == "coarse_blocked" and not (
                coarse == STAGED_FULL_REASON and (v.get("fine_reason") or candidate.get("fine_reason"))
            ) else str(
                candidate.get("fine_reason") or v.get("fine_reason")
                               or (v.get("why") if str(v.get("status")) == "rejected"
                                   else "") or ""),
            "first_seen": str(v.get("first_seen") or ""),
            "last_activity": last_activity,
            "queue_state": queue_state,
            "final_result": result,
            "pr_url": pr_url,
            "pr_state": (pr_states or {}).get(pr_url),
        })
    rows.sort(key=lambda r: r["first_seen"], reverse=True)
    return rows


# ---- 候选池/扫描发现的入队状态：黄=排队中，绿=已解决，红=已失效 ----

_QUEUE_YELLOW = {"staged", "needs_maintainer_confirm", "needs_cla",
                 "gated_alignment_only", "running"}
_QUEUE_GREEN = {"pr_open", "merged", "completed"}
_QUEUE_RED = {"aborted_stale", "stage_expired", "escalated", "aborted_competed",
              "aborted_duplicate", "aborted_closed", "aborted_no_evidence",
              "aborted_blocked_design", "aborted_pipeline", "coarse_blocked", "fine_blocked",
              "screen_skipped", "queue_pruned", "queue_evicted"}
QUEUE_EXPIRE_DAYS = 3


def queue_display_state(status: str, updated_at: str = "") -> str:
    """候选的处理状态 → 飞书三色语义。

    黄=排队中（含等确认/等CLA），绿=已解决（PR 已开出），红=已失效
    （stale/竞争失败/尝试超限，或 new/staged 躺了超过 3 天没轮上）。
    """
    status = str(status or "")
    import datetime as _dt
    if status in _QUEUE_GREEN:
        return "已解决"
    if status in _QUEUE_RED:
        return "已失效"
    if status in _QUEUE_YELLOW:
        # Expiration is a real queue transition owned by the dispatcher. An old
        # updated_at must not hide work that was just deferred to tomorrow.
        return "排队中"
    return ""


def recheck_still_worth(key: str, title: str = "") -> tuple[bool, str]:
    """粗筛：这条 staged 候选还值得做吗（凌晨批量用）。

    规则层确定性检查：issue 是否仍 open、无人认领、无关联 PR、不是重复。
    有人做了/已关闭 → (False, 原因)。调用失败保守放行（宁重评不误杀）。
    """
    repo, _, num = key.partition("#")
    if not repo or not num.isdigit():
        return False, "bad key"
    try:
        from scout_v2.git_host import recheck_candidate
        r = recheck_candidate(repo, int(num), root_cause_keywords=title or None)
        if r is None or not r.complete:
            return True, "incomplete recheck (kept)"
        if not r.issue_open:
            return False, f"issue closed (state={r.issue_open})"
        if not r.unassigned:
            return False, "already assigned"
        if r.has_claim_signal:
            return False, "claim signal in comments/body"
        if r.is_duplicate:
            return False, f"duplicate of {r.duplicate_of}"
        if r.has_cross_ref_pr or r.open_pr_hits:
            return False, f"PR exists: {(r.open_pr_hits or ['xref'])[0]}"
        return True, "ok"
    except Exception as e:  # noqa: BLE001
        return True, f"recheck error (kept): {str(e)[:100]}"


_SCAN_OTHER_OUTCOMES = {
    "queue_evicted": "已清除",
    "stage_expired": "暂存已过期",
    "escalated": "待人工核查",
    "needs_cla": "开工拦截 · 需要签署 CLA",
    "needs_maintainer_confirm": "开工拦截 · 等待维护者确认",
    "gated_alignment_only": "开工拦截 · 仓库要求先沟通",
    "observe_only": "开工拦截 · 仓库仅观察",
    "out_of_scope": "开工拦截 · 仓库不在范围内",
}


def candidate_activity_state(key: str, status: str, running_keys) -> str:
    if key.lower() in running_keys and status in {"staged", "running", "escalated"}:
        return "执行中"
    if status in {"staged", "running"}:
        return "暂存"
    return ""


def build_candidate_rows(root, running_keys=()) -> list[dict]:
    """候选池只展示可执行、暂存或正在派发/执行的候选。"""
    qpath = root / "queue" / "candidates.json"
    try:
        queue = json.loads(qpath.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    rows = []
    for key, entry in sorted(queue.items()):
        display = candidate_activity_state(key, entry.get("status"), running_keys)
        if not display:
            continue
        rows.append({
            "key": key,
            "title": str(entry.get("title") or "")[:300],
            "queue_state": display,
            "discovered_at": entry.get("discovered_at"),
            "staged_at": entry.get("staged_at"),
        })
    return rows
