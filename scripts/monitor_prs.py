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
ATTEMPTS_FILE = ROOT / "state" / "pr_fix_attempted.json"


def pr_head(repo: str, number: int) -> dict:
    raw = _gh(["pr", "view", str(number), "-R", repo, "--json",
               "headRefName,headRefOid,headRepositoryOwner,isCrossRepository"])
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
    """log 里有没有真实的测试/编译失败痕迹（区别于纯权限提示）。"""
    return bool(re.search(
        r"FAILED|AssertionError|Traceback|error\[|error:|Build failed|"
        "compilation error|panic:|exit code [1-9]", text, re.I))


def _attempts() -> dict:
    try:
        return json.loads(ATTEMPTS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def dispatch_pr_fix(repo: str, number: int, *, dry_run: bool = False) -> dict:
    """对挂红的 PR 派一轮 pr-fix。返回 {dispatched, reason, ...}。

    去重键 = (repo, number, head_sha)：派一次就记；修复推了新 commit、head
    变了才允许对新的失败再派。外因不派、非我们的 fork 不派、已在修的不派。
    """
    head = pr_head(repo, number)
    if not head:
        return {"dispatched": False, "reason": "pr head unavailable"}
    if not head.get("isCrossRepository") or \
            (head.get("headRepositoryOwner") or {}).get("login") != "xixi-box":
        return {"dispatched": False, "reason": "head not on our fork"}

    head_sha = head.get("headRefOid", "")
    key = f"{repo}#{number}@{head_sha[:12]}"
    attempts = _attempts()
    if key in attempts:
        return {"dispatched": False, "reason": f"already attempted: {key}"}

    failing = failing_details(repo, number)
    ours, why = failure_is_ours(failing)
    if not ours:
        attempts[key] = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                         "dispatched": False, "reason": why}
        _save_attempts(attempts)
        return {"dispatched": False, "reason": why}

    if dry_run:
        return {"dispatched": False, "reason": f"dry-run: would fix ({why})"}

    # bot 评论一起带给 agent：changeset 缺失这类机械要求就是修复项（L2）
    raw = _gh(["api", f"repos/{repo}/issues/{number}/comments", "--jq",
               r'.[] | select(.body | test("changeset|changelog|CLAUDE|coderabbit|'
               r'sonarqube|lint|format"; "i")) | "\(.user.login)\t\(.body)"'])
    comments = []
    for line in (raw or "").splitlines()[:8]:
        parts = line.split("\t", 1)
        if len(parts) == 2:
            comments.append({"author": parts[0], "body": parts[1][:600]})

    payload = {"fork_repo": f"xixi-box/{repo.split('/')[1]}",
               "branch": head.get("headRefName", ""),
               "upstream_repo": repo, "pr_number": number,
               "head_sha": head_sha,
               "failing": failing[:6], "comments": comments}
    import base64 as _b64
    fix_b64 = _b64.b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode()
    job_id = f"prfix-{repo.replace('/', '-')}-{number}-{head_sha[:8]}"
    # _gh 失败返回 ""，但 workflow run 成功时 stdout 也是空——用 run_gh 拿 rc
    rc = run_gh(["workflow", "run", "oss-scout-pr-fix.yml", "-R", "xixi-box/oss-scout",
                 "-f", f"fix_b64={fix_b64}", "-f", f"job_id={job_id}"], timeout=60)
    ok = rc == 0
    attempts[key] = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                     "dispatched": ok,
                     "reason": why if ok else "dispatch failed"}
    _save_attempts(attempts)
    return {"dispatched": ok, "reason": why, "job_id": job_id}


def _save_attempts(data: dict) -> None:
    ATTEMPTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    # 只留最近 200 条，防无限膨胀
    if len(data) > 200:
        data = dict(sorted(data.items(), key=lambda kv: kv[1].get("ts", ""))[-200:])
    ATTEMPTS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                             encoding="utf-8")


# ---- L3：回复草拟（只起草，绝不自动发）----

REPLIES_FILE = ROOT / "state" / "pr_replies.json"


def _thread(repo: str, number: int, limit: int = 10) -> list[dict]:
    raw = _gh(["api", f"repos/{repo}/issues/{number}/comments",
               "--jq", '.[] | "\(.created_at)|\(.user.login)|\(.body)"'])
    out = []
    for line in (raw or "").splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3:
            out.append({"created_at": parts[0], "author": parts[1],
                        "body": parts[2][:1500]})
    return out[-limit:]


def draft_reply(repo: str, number: int, *, force: bool = False) -> dict:
    """AI 起草对最新未回评论的回复，存 state/pr_replies.json。

    只起草不发送：外发文字过审是人的事（Playbook §2①，astral/biome 前车之鉴）。
    同一 PR 已有未发送草稿且评论没更新时跳过（省一次模型调用）。
    """
    try:
        replies = json.loads(REPLIES_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        replies = {}
    key = f"{repo}#{number}"
    thread = _thread(repo, number)
    if not thread:
        return {"drafted": False, "reason": "no comments"}
    latest = thread[-1]
    prev = replies.get(key) or {}
    if (not force and prev.get("status") == "draft"
            and prev.get("for_comment") == latest.get("created_at")):
        return {"drafted": False, "reason": "draft already current"}

    # 只带我们上一次发言之后的内容：更早的上下文模型不需要全部
    try:
        from scout_v2.workers.repair import resolve_model
        model = resolve_model()
    except Exception as e:  # noqa: BLE001
        return {"drafted": False, "reason": f"no model: {e}"}
    from scout_v2.workers.issue_scorer import GoClient, load_scorer_config
    cfg = load_scorer_config(ROOT)
    if not cfg.get("api_key"):
        return {"drafted": False, "reason": "no scorer api_key"}

    convo = "\n".join(f"[{c['author']}] {c['body'][:500]}" for c in thread[-6:])
    prompt = (
        "You maintain an open-source contribution bot. A maintainer or bot replied "
        "on our pull request. Draft the reply a polite human contributor would "
        "write.\n"
        "Rules: max 6 lines; concrete and technical; no promises about timing; "
        "no AI disclaimers; if the comment asks for a code change, acknowledge and "
        "say what will change (the fix itself is handled separately); plain text, "
        "no markdown headers.\n\n"
        f"Conversation on {repo}#{number}:\n{convo}\n\n"
        "Reply with ONLY the reply text.")

    client = GoClient(key=cfg["api_key"], model=cfg.get("model", "deepseek-v4.1-flash"),
                      timeout=180)
    ok, detail = client.chat(prompt)
    if not ok:
        return {"drafted": False, "reason": f"model error: {detail[:120]}"}

    replies[key] = {"status": "draft",
                    "for_comment": latest.get("created_at"),
                    "to_author": latest.get("author"),
                    "draft": detail.strip()[:1200],
                    "drafted_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    REPLIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    REPLIES_FILE.write_text(json.dumps(replies, ensure_ascii=False, indent=1),
                            encoding="utf-8")
    return {"drafted": True, "key": key}


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
