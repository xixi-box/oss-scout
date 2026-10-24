#!/usr/bin/env python3
"""oss-scout 发现层：对监控名单做确定性 issue 发现与资格门过滤。

纯 GitHub Search/REST API（经 gh CLI 认证），无状态重扫：每次全量拉取，
state/seen.json 只负责通知去重。发现层只新增 queue 条目；消费者只改 status。

用法:
  python discover.py                     # 全量跑（~5 分钟，~220 个 API 调用）
  python discover.py --repos org/name,…  # 只跑指定仓库
  python discover.py --max-enrich 3      # 每仓库最多深查 N 条（默认取 config）
  python discover.py --dry-run           # 只打印，不写文件、不发通知
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"
QUEUE = ROOT / "queue"
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
SEARCH_CFG = CONFIG.get("search", {})

EXCLUDED_LABELS = SEARCH_CFG.get("excluded_labels", [])
FRESH_DAYS = int(SEARCH_CFG.get("fresh_days", 45))
MAX_ENRICH = int(SEARCH_CFG.get("max_enrich_per_repo", 5))

# ---- 信号正则（脚本只负责给信号和粗分，最终 A/B/C 判断归消费者 agent）----
RE_CLAIM = re.compile(
    r"\bi\s?(?:'ll|will|'m\s+(?:working|on\s+it)|'d\s+like\s+to\s+(?:work|take|fix))\b"
    r"|\bworking\s+on\s+(?:this|a\s+fix|it)\b"
    r"|\btaking\s+(?:this|it)\b|\bpick(?:ed|ing)?\s+(?:this|it)\s?up\b"
    r"|\bPR\s+(?:is\s+)?(?:open|coming|up)\b|\bopened?\s+a\s+(?:PR|pull\s+request)\b",
    re.I,
)
RE_CONFIRM = re.compile(
    r"\b(confirm(?:ed|s)?|reproduc(?:ed|ible|es)|thanks\s+for\s+the\s+report|"
    r"this\s+is\s+(?:a\s+)?(?:bug|indeed)|we\s+(?:should|need\s+to)\s+fix)\b",
    re.I,
)
RE_REPRO = re.compile(
    r"\b(?:steps?\s+to\s+(?:reproduce|repro)|traceback|minimal\s+(?:repro|example)|"
    r"reproduction\s+steps|expected\s+.{0,40}actual)\b",
    re.I,
)
RE_BAD_TITLE = re.compile(
    r"^(how\s+(?:do|to|can)\b|is\s+it\s+possible|any\s+(?:plan|way|update)|"
    r"\[?question\]?|\[?feature\s+(?:request|idea)\]?|support\s+for\b|roadmap|"
    r"support\b|add\s+(?:support|ability|option))",
    re.I,
)
RE_FEATURE_LABEL = re.compile(r"enhancement|feature|rfc|proposal|design|discussion|idea", re.I)
RE_DOCS = re.compile(r"\b(typo|doc(?:umentation|s)?|readme|changelog|comment\s+fix)\b", re.I)
RE_DUPLICATE = re.compile(
    r"duplicate of #\d+"
    r"|already tracked by (?:several |multiple |a few )?(?:open |other |existing )?issues?"
    r"|identical (?:crash|issue|bug|error) (?:already )?(?:tracked|reported)",
    re.I,
)
# 官方 AI bot 认领信号（如 OpenHands 的 openhands-ai[bot] "I'm on it!"——官方智能体收割自家 issue 队列）
RE_BOT_CLAIM = re.compile(r"I'?m on it|tracking my progress", re.I)
# 计费/配额投诉簇：服务端计量问题，代码侧不可修（opencode Go 计划一类的 issue 族）
RE_BILLING = re.compile(r"\b(quota|billing|subscription|refund|scam|re-?charge[d]?|plan limit)\b", re.I)
MAINTAINER_ASSOC = {"MEMBER", "OWNER", "COLLABORATOR"}


def gh(args: list[str], max_retries: int = 3):
    """调用 gh api。args 里标志外的路径必须已是单个字符串。二次限流时退避重试。"""
    cmd = ["gh", "api"] + args
    err = ""
    for attempt in range(max_retries + 1):
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if p.returncode == 0:
            return json.loads(p.stdout)
        err = p.stderr.strip()
        if "422" in err or "Validation Failed" in err:
            return None
        if "secondary rate limit" in err.lower():
            time.sleep(25 * (attempt + 1))   # GitHub 反爬退避，硬等
            continue
        if attempt < max_retries:
            time.sleep(3 * (attempt + 1))
    print(f"    [gh] {err[:150]}", file=sys.stderr)
    return None


def search_issues(repo: str, extra: str, sort: str, per_page: int = 30):
    q = f"repo:{repo} is:issue is:open no:assignee {extra}"
    res = gh(["-X", "GET", "search/issues", "-f", f"q={q}",
              "-f", f"per_page={per_page}", "-f", f"sort={sort}", "-f", "order=desc"])
    time.sleep(1.0)
    return (res or {}).get("items", [])


def label_fragment() -> str:
    return " ".join(f'-label:"{lab}"' for lab in EXCLUDED_LABELS)


def candidate_universe(repo: str):
    """三路搜索：高互动（reactions）+ 新 bug（created 45d）+ 新鲜未认领泳道（2~14 天龄 bug）。

    第三路是数据验证的主战场：零反应数、躺 2~14 天没人捡、被捡后 maintainer 小时级合并。
    返回 (pool, fresh_ids)：fresh_ids 里的条目在深查排序中优先于 reactions 泳道。
    """
    pool: dict[int, dict] = {}
    fresh_ids: set[int] = set()
    for extra, sort in [
        (label_fragment(), "reactions"),
        (f'label:bug {label_fragment()}', "created"),
    ]:
        for it in search_issues(repo, extra, sort):
            pool[it["number"]] = it
    upper = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)).date().isoformat()
    lower = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=14)).date().isoformat()
    for it in search_issues(repo,
            f'label:bug {label_fragment()} created:{lower}..{upper}', "created"):
        pool[it["number"]] = it
        fresh_ids.add(it["number"])
    return pool, fresh_ids


def enrich(repo: str, number: int) -> dict | None:
    """深查单条：正文互动、评论认领信号、maintainer 确认、关联 PR。"""
    issue = gh([f"repos/{repo}/issues/{number}"])
    time.sleep(1.0)
    if issue is None:
        return None
    comments = gh([f"repos/{repo}/issues/{number}/comments?per_page=30"])
    time.sleep(1.0)
    timeline = gh([f"repos/{repo}/issues/{number}/timeline?per_page=100"])
    time.sleep(1.0)
    if comments is None or timeline is None:
        # 抓取失败=信号不完整：宁可漏一条也不能带着假"无竞争"结论入库（#8415 教训）
        return None

    claim_signal = False
    maintainer_confirm = False
    duplicate_flag = False
    billing_flag = False
    bot_claim = False
    for c in comments:
        login = (c.get("user") or {}).get("login", "")
        body = c.get("body") or ""
        # 以下信号对 bot 评论同样有效（duplicate 指针来自 github-actions[bot]，
        # 官方 bot 认领来自 openhands-ai[bot]——恰恰都发在 bot 评论里）
        if RE_DUPLICATE.search(body):
            duplicate_flag = True
        if RE_BOT_CLAIM.search(body):
            bot_claim = True
        if RE_BILLING.search(body):
            billing_flag = True
        # 人类认领/维护者确认只看非 bot 评论
        if login.endswith("[bot]") or login.endswith("bot"):
            continue
        if RE_CLAIM.search(body):
            claim_signal = True
        if c.get("author_association") in MAINTAINER_ASSOC and RE_CONFIRM.search(body):
            maintainer_confirm = True

    cross_ref_pr = False
    for ev in timeline:
        if isinstance(ev, dict) and ev.get("event") == "cross-referenced":
            src = (ev.get("source") or {}).get("issue") or {}
            if src.get("pull_request"):
                cross_ref_pr = True
                break

    return {
        "body_len": len(issue.get("body") or ""),
        "reactions": (issue.get("reactions") or {}).get("total_count", 0),
        "labels": sorted(lb["name"] for lb in issue.get("labels") or []),
        "comments": issue.get("comments", 0),
        "claim_signal": claim_signal,
        "maintainer_confirm": maintainer_confirm,
        "duplicate_flag": duplicate_flag,
        "billing_flag": billing_flag,
        "bot_claim": bot_claim,
        "cross_ref_pr": cross_ref_pr,
        "repro_signal": bool(RE_REPRO.search(issue.get("body") or "")),
    }


def score(title: str, sig: dict, created_at: str, lane: str) -> tuple[int, str, list[str]]:
    """返回 (score, class, blockers)。blocker 直接 C。"""
    blockers: list[str] = []
    s = 0
    if sig["maintainer_confirm"]:
        s += 3
    if sig["reactions"] >= 5:
        s += 3
    elif sig["reactions"] >= 1:
        s += 1
    if "bug" in [lb.lower() for lb in sig["labels"]]:
        s += 2
    if "help wanted" in [lb.lower() for lb in sig["labels"]]:
        s += 2
    if sig["repro_signal"]:
        s += 2
    age_days = (dt.datetime.now(dt.timezone.utc)
                - dt.datetime.fromisoformat(created_at.replace("Z", "+00:00"))).days
    if age_days <= 7:
        s += 1
    if sig["comments"] <= 3:
        s += 1

    if sig["cross_ref_pr"]:
        blockers.append("cross_ref_pr")
    if sig["claim_signal"]:
        blockers.append("claim_signal")
    if sig.get("duplicate_flag"):
        blockers.append("duplicate_flagged")
    if sig.get("bot_claim"):
        blockers.append("org_bot_claimed")
    if RE_BILLING.search(title):
        blockers.append("billing_complaint")
    if any(RE_FEATURE_LABEL.search(lb) for lb in sig["labels"]):
        blockers.append("feature_label")
    if sig["body_len"] < 200:
        blockers.append("thin_body")
    if RE_BAD_TITLE.search(title):
        blockers.append("question_or_feature_title")
    if RE_DOCS.search(title):
        blockers.append("docs_only")

    if lane == "restricted" and not (sig["maintainer_confirm"]
                                     or "help wanted" in [lb.lower() for lb in sig["labels"]]):
        blockers.append("restricted_requires_confirm")

    if blockers:
        return s, "C", blockers
    return s, ("A" if s >= 6 else "B" if s >= 3 else "C"), []


def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def atomic_write(path: Path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def feishu_notify(text: str, dry: bool):
    hook_file = STATE / "feishu_webhook.txt"
    if dry or not hook_file.exists():
        return False
    url = hook_file.read_text(encoding="utf-8").strip()
    if not url:
        return False
    try:
        req = urllib.request.Request(
            url, data=json.dumps({"msg_type": "text", "content": {"text": text}}).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as e:  # 通知失败不影响主流程
        print(f"    [feishu] 通知失败: {e}", file=sys.stderr)
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repos", help="逗号分隔，只跑这些仓库")
    ap.add_argument("--max-enrich", type=int, default=MAX_ENRICH)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    repos: list[tuple[str, str]] = ([(r["repo"], "open") for r in CONFIG["open_pool"]]
                                    + [(r["repo"], "gated") for r in CONFIG["gated_pool"]]
                                    + [(r["repo"], "restricted") for r in CONFIG["restricted_pool"]])
    if args.repos:
        want = {r.strip() for r in args.repos.split(",")}
        repos = [(r, l) for r, l in repos if r in want]

    seen = load_json(STATE / "seen.json", {})
    queue = load_json(QUEUE / "candidates.json", {})
    now_iso = dt.datetime.now().isoformat(timespec="seconds")
    new_entries: list[dict] = []
    report_lines: list[str] = []

    for repo, lane in repos:
        print(f"== {repo} [{lane}]")
        try:
            pool, fresh_ids = candidate_universe(repo)
        except Exception as e:
            print(f"    搜索失败，跳过: {e}", file=sys.stderr)
            continue
        if not pool:
            print("    无原始候选")
            continue

        cfg_entry = next((c for c in CONFIG["open_pool"] + CONFIG["gated_pool"]
                          + CONFIG["restricted_pool"] if c["repo"] == repo), {})
        me = cfg_entry.get("max_enrich") or args.max_enrich

        # 深查顺序：新鲜泳道（2~14 天龄，越新越急）优先，其余按 reactions、并列时越新越优先
        def rough_key(it):
            try:
                c = -dt.datetime.fromisoformat(
                    it.get("created_at", "1970-01-01T00:00:00Z").replace("Z", "+00:00")).timestamp()
            except Exception:
                c = 0.0
            return (-(it.get("reactions") or {}).get("total_count", 0), c, it["number"])
        fresh_sorted = sorted((pool[n] for n in fresh_ids if n in pool),
                              key=lambda it: it.get("created_at", ""), reverse=True)
        rest = sorted((it for n, it in pool.items() if n not in fresh_ids), key=rough_key)
        top = (fresh_sorted + rest)[:me]

        new_cnt = 0
        for it in top:
            key = f"{repo}#{it['number']}"
            if key in seen:      # 已见过：永不重复通知/入队
                continue
            sig = enrich(repo, it["number"])
            if sig is None:
                continue          # API 失败不标记 seen，下轮重查
            s, sclass, blockers = score(it["title"], sig, it["created_at"], lane)
            if sclass == "C":
                # 被阻断（有竞争 PR/认领/feature 性质）或纯低分：记 seen 防重查，不入队
                seen[key] = {"first_seen": now_iso,
                             "status": "blocked" if blockers else "lowscore"}
                print(f"    [C  {s:2d}] #{it['number']} {it['title'][:70]}"
                      + (f"  ←{blockers}" if blockers else ""))
                continue
            entry = {
                "id": key, "repo": repo, "number": it["number"],
                "title": it["title"], "url": it["html_url"],
                "lane": lane, "score": s, "class": sclass,
                "blockers": blockers, "signals": sig,
                "created_at": it["created_at"], "discovered_at": now_iso,
                "status": "new", "lease": None,
            }
            new_entries.append(entry)
            queue[key] = entry
            seen[key] = {"first_seen": now_iso, "status": "queued"}
            new_cnt += 1
            print(f"    [{sclass} {s:2d}] #{it['number']} {it['title'][:70]}")
        print(f"    原始池 {len(pool)}，新入库 {new_cnt}")

    if not args.dry_run:
        atomic_write(STATE / "seen.json", seen)
        atomic_write(QUEUE / "candidates.json", queue)

    # 摘要 + 通知（只推 A/B 级）
    ab = [e for e in new_entries if e["class"] in ("A", "B")]
    summary = (f"[oss-scout {now_iso}] 新候选 {len(new_entries)}"
               f"（A: {sum(1 for e in new_entries if e['class']=='A')}, "
               f"B: {len(ab) - sum(1 for e in new_entries if e['class']=='A')}）\n"
               + "\n".join(f"[{e['class']}/{e['lane']}] {e['repo']}#{e['number']} {e['title'][:60]}\n{e['url']}"
                           for e in ab[:8]))
    print("\n" + summary)
    if new_entries and feishu_notify(summary, args.dry_run):
        print("(已推送飞书)")


if __name__ == "__main__":
    main()
