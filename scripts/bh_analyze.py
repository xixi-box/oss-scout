"""BH findings 分析：AI 逐条判定 + 生成人工评审队列（只读外部，落盘本仓库）。

链路位置：p6_bug_hunter 扫出 findings → **本脚本** → state/bughunt/review.md
设计（用户裁定）：AI 判真伪，**人决定是否开 issue**——绝不自动开。

对每条 finding 构造判定 prompt（含代码片段），用 GoClient（deepseek）判定：
  verdict: real_bug / likely_noise / needs_more_context
  reason: 一句话
输出：
  state/bughunt/verdicts.json   逐条判定（机器可读，供后续开 issue 用）
  state/bughunt/review.md       人工评审表（按 verdict 排序）

用法（容器内）：
  python /app/scripts/bh_analyze.py [--limit 20] [--repo REPO]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

BUGHUNT = Path("/data/state/bughunt")


def _load_scorer():
    sys.path.insert(0, "/app")
    from scout_v2.workers.issue_scorer import GoClient, load_scorer_config

    cfg = load_scorer_config(Path("/data"))
    if not cfg.get("api_key"):
        return None
    return GoClient(key=cfg["api_key"], model=cfg.get("model", "deepseek-v4.1-flash"),
                    timeout=180, base_url=cfg.get("base_url"))


def _prompt(finding: dict, repo: str) -> str:
    return f"""You are triaging a candidate defect found by static analysis + test-marker mining in {repo}.

Finding: {finding.get('message', '')[:300]}
Location: {finding.get('path', '?')}:{finding.get('line', 0)}
Kind: {finding.get('rule_id', '?')}

Code around it:
{finding.get('snippet', '')[:1200]}

Question: is this a real defect worth a maintainer's time, likely noise, or
needs more context? For xfail/skip markers: does the stated reason describe an
actual product defect (not just a platform skip or flaky test)?

Answer in EXACTLY this format:
VERDICT: real_bug | likely_noise | needs_more_context
REASON: <one sentence, max 40 words>"""


def _parse(text: str) -> tuple[str, str]:
    verdict, reason = "needs_more_context", text[:200]
    for line in text.splitlines():
        low = line.strip().lower()
        if low.startswith("verdict:"):
            v = line.split(":", 1)[1].strip().lower()
            if "real" in v:
                verdict = "real_bug"
            elif "noise" in v:
                verdict = "likely_noise"
            elif "context" in v:
                verdict = "needs_more_context"
        elif low.startswith("reason:"):
            reason = line.split(":", 1)[1].strip()
    return verdict, reason


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--repo", default=None, help="只分析指定仓库")
    ap.add_argument("--verdict", default=None, help="只重分析某 verdict 的条目")
    args = ap.parse_args()

    client = _load_scorer()
    if client is None:
        print("no scorer configured (OPENCODE_API_KEY)")
        return 2

    files = sorted(BUGHUNT.glob("*/*/findings.json"))
    if args.repo:
        files = [f for f in files if args.repo in str(f)]

    # 已有判定：跳过（重跑不重复烧钱）
    vpath = BUGHUNT / "verdicts.json"
    try:
        verdicts = json.loads(vpath.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        verdicts = {}

    rows, analyzed, skipped = [], 0, 0
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        repo = d.get("repo", f.parts[-3])
        for x in d.get("findings") or []:
            fp = x.get("fingerprint") or ""
            key = f"{repo}:{fp}"
            prev = verdicts.get(key)
            if prev and not args.verdict:
                rows.append({**x, "repo": repo, "verdict": prev["verdict"],
                             "reason": prev["reason"]})
                skipped += 1
                continue
            if args.verdict and prev and prev["verdict"] != args.verdict:
                rows.append({**x, "repo": repo, "verdict": prev["verdict"],
                             "reason": prev["reason"]})
                continue
            if analyzed >= args.limit:
                rows.append({**x, "repo": repo, "verdict": "unanalyzed",
                             "reason": "budget cap"})
                continue
            ok, detail = client.chat(_prompt(x, repo))
            verdict, reason = _parse(detail) if ok else ("error", detail[:200])
            verdicts[key] = {"verdict": verdict, "reason": reason,
                             "repo": repo, "path": x.get("path"),
                             "line": x.get("line")}
            rows.append({**x, "repo": repo, "verdict": verdict, "reason": reason})
            analyzed += 1
            print(f"  [{verdict}] {repo} {x.get('path')}:{x.get('line')} :: {reason[:80]}")
            time.sleep(0.5)

    vpath.parent.mkdir(parents=True, exist_ok=True)
    vpath.write_text(json.dumps(verdicts, ensure_ascii=False, indent=1),
                     encoding="utf-8")

    # 人工评审表
    order = {"real_bug": 0, "needs_more_context": 1, "error": 2,
             "unanalyzed": 3, "likely_noise": 4}
    rows.sort(key=lambda r: order.get(r.get("verdict", "unanalyzed"), 9))
    md = ["# Bug Hunter 评审队列", "",
          f"共 {len(rows)} 条 | 新分析 {analyzed} | 复用 {skipped}",
          "| verdict | 仓库 | 位置 | 类型 | 理由 |",
          "|---|---|---|---|---|"]
    for r in rows:
        md.append(f'| {r.get("verdict")} | {r.get("repo")} | '
                  f'`{r.get("path")}:{r.get("line")}` | '
                  f'{r.get("rule_id")} | {str(r.get("reason"))[:150]} |')
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.get("verdict", "?")] = counts.get(r.get("verdict", "?"), 0) + 1
    md.append("")
    md.append(f"**统计**：{json.dumps(counts, ensure_ascii=False)}")
    md.append("")
    md.append("真 bug 的候选需要人工复核后才可开 issue（绝不自动开）。")
    (BUGHUNT / "review.md").write_text("\n".join(md), encoding="utf-8")

    print(f"\ndone: analyzed={analyzed} reused={skipped} "
          f"verdicts={json.dumps(counts, ensure_ascii=False)}")
    print(f"review queue: {BUGHUNT / 'review.md'}")
    return 0


if __name__ == "__main__":
    main()
