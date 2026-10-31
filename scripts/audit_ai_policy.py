"""检查池子里每个仓库对自动化/AI 贡献的政策。

2026-10-30 实录：一天开出 12 个 PR 之后被关掉 3 个，原因各不相同但都不是
代码问题：
  - astral-sh/uv     组织级 AI_POLICY.md 明文禁止自主 agent 贡献，bot 当场关
  - langchain        闸门仓库，未被指派就开 PR → require-issue-link 自动关
  - biome            CONTRIBUTING 写明 AI 生成的低信号描述可被直接关闭，
                     且装了 AgentScan 把我们标成 "7 repos forked in 24h"
这些规则散落在 CONTRIBUTING.md / AI_POLICY.md / 组织级仓库里，靠人读是读不完
的。这个脚本把它们扫出来，作为加仓库前的闸门。

用法：
    python scripts/audit_ai_policy.py            # 扫 configs 里的池子
    python scripts/audit_ai_policy.py --json     # 机器可读
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 出现这些词就认为该仓库对自动化贡献有额外要求，需要人看一眼原文
CANDIDATE_PATHS = [
    "AI_POLICY.md", ".github/AI_POLICY.md", "AI_POLICY",
    "CONTRIBUTING.md", ".github/CONTRIBUTING.md",
    "docs/CONTRIBUTING.md", "docs/development/contributing.mdx",
    ".github/pull_request_template.md",
]

# 判定用：命中的片段原样回传，让人自己读，不猜
SIGNALS = [
    (re.compile(r"autonomous\s+agent", re.I), "forbids_autonomous_agents"),
    (re.compile(r"we\s+do\s+not\s+allow\s+autonomous", re.I), "forbids_autonomous_agents"),
    (re.compile(r"AI[-\s]?generated\s+communication", re.I), "restricts_ai_wording"),
    (re.compile(r"AI\s+should\s+not\s+be\s+used\s+to\s+communicate", re.I), "restricts_ai_wording"),
    (re.compile(r"must\s+be\s+assigned\s+to\s+(?:that|the)\s+issue", re.I), "requires_assignment"),
    (re.compile(r"not\s+assigned\s+to\s+the\s+linked\s+issue", re.I), "requires_assignment"),
    (re.compile(r"require-issue-link", re.I), "requires_assignment"),
    (re.compile(r"(\d+)\+?\s+merged\s+PRs?", re.I), "merged_pr_threshold"),
    (re.compile(r"bulk\s+automated|automated\s+changes", re.I), "closes_bulk_automation"),
]


def _gh(args: list[str]) -> str:
    r = subprocess.run(["gh"] + args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=90)
    return r.stdout if r.returncode == 0 else ""


def _fetch(repo: str, path: str) -> str:
    raw = _gh(["api", f"repos/{repo}/contents/{path}"])
    if not raw:
        return ""
    try:
        return base64.b64decode(json.loads(raw)["content"]).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""


def audit(repo: str) -> dict:
    findings: list[dict] = []
    for path in CANDIDATE_PATHS:
        text = _fetch(repo, path)
        if not text:
            continue
        for pat, tag in SIGNALS:
            m = pat.search(text)
            if not m:
                continue
            start = max(0, m.start() - 120)
            findings.append({"file": path, "tag": tag,
                             "snippet": " ".join(text[start:m.end() + 200].split())})
    tags = sorted({f["tag"] for f in findings})
    # 只有"禁止自主 agent"在我这里是硬否决；其余是人工复核项
    verdict = ("excluded" if "forbids_autonomous_agents" in tags
               else "review" if tags else "ok")
    return {"repo": repo, "verdict": verdict, "tags": tags, "findings": findings}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--repos", nargs="*", help="defaults to configs/repositories.yaml")
    args = ap.parse_args()

    if args.repos:
        repos = args.repos
    else:
        import yaml
        cfg = yaml.safe_load((ROOT / "configs" / "repositories.yaml")
                             .read_text(encoding="utf-8"))
        repos = [r for r in (cfg.get("repositories") or {})
                 if not r.startswith("xixi-box/")]

    results = [audit(r) for r in repos]
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=1))
    else:
        for res in results:
            if res["verdict"] == "ok":
                continue
            print(f"{res['verdict']:9s} {res['repo']:38s} {','.join(res['tags'])}")
            for f in res["findings"][:3]:
                print(f"    [{f['file']}] {f['snippet'][:220]}")
        bad = [r for r in results if r["verdict"] == "excluded"]
        print(f"\n{len(results)} repos scanned, {len(bad)} excluded, "
              f"{len([r for r in results if r['verdict'] == 'review'])} need review")
    # 有硬否决就以非零退出，便于接进 CI
    return 1 if any(r["verdict"] == "excluded" for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
