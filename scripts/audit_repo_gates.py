"""池子仓库的贡献门槛普查（只读）。

2026-10-30 的教训是：合并率高不代表能进池。一天被关 3 个 PR，全是规则问题而非
代码问题。这个脚本把「能不能往这个仓库提 PR」的硬性条件扫出来：

  1. AI / 自动化政策（禁止自主 agent、禁止 AI 写沟通）
  2. 未指派即自动关 PR（闸门）
  3. 作者门槛（要求先有 N 个已合并 PR、要求 CLA/DCO）
  4. 装了什么自动审核机器人（AgentScan 之类会识别自动化行为）

用法：
    python scripts/audit_repo_gates.py            # 全池
    python scripts/audit_repo_gates.py --json
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

POLICY_FILES = [
    "AI_POLICY.md", ".github/AI_POLICY.md",
    "CONTRIBUTING.md", ".github/CONTRIBUTING.md",
    "docs/CONTRIBUTING.md", "docs/development/contributing.mdx",
    "CONTRIBUTING.rst",
]

# 命中即排除或强烈警示的规则
RULES = [
    ("forbid_agent", re.compile(
        r"(?:do\s+not|don'?t|not)\s+allow\s+(?:autonomous\s+)?agents?|"
        r"autonomous\s+agents?\s+(?:are\s+)?(?:not|prohibited)|"
        r"we\s+do\s+not\s+allow\s+autonomous", re.I)),
    ("ai_wording", re.compile(
        r"AI[-\s]?generated\s+(?:communication|comment|description|text)|"
        r"AI\s+should\s+not\s+be\s+used\s+to\s+communicate", re.I)),
    ("assignment_gate", re.compile(
        r"must\s+be\s+assigned\s+to|not\s+assigned\s+to\s+the\s+linked|"
        r"require-issue-link|assignment\s+is\s+required\s+before", re.I)),
    ("merged_pr_threshold", re.compile(
        r"(\d+)\+?\s+merged\s+PRs?", re.I)),
    ("cla_required", re.compile(
        r"Contributor\s+License\s+Agreement|\bCLA\b", re.I)),
    ("dco_required", re.compile(r"\bDCO\b|Signed-off-by", re.I)),
    ("bulk_automation_rejected", re.compile(
        r"bulk\s+automated|automated\s+(?:changes|PRs?)\s+(?:will\s+be|are)\s+closed|"
        r"AI-generated\s+changes", re.I)),
]

# 会识别自动化行为的机器人
SCANNER_BOTS = ["agentscan", "ai-detector", "plagiarism", "sweep"]


def _gh(args: list[str]) -> str:
    r = subprocess.run(["gh"] + args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=90)
    return r.stdout if r.returncode == 0 else ""


def _file(repo: str, path: str) -> str:
    raw = _gh(["api", f"repos/{repo}/contents/{path}"])
    if not raw:
        return ""
    try:
        return base64.b64decode(json.loads(raw)["content"]).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""


def audit(repo: str) -> dict:
    hits: list[dict] = []
    for path in POLICY_FILES:
        text = _file(repo, path)
        if not text:
            continue
        for tag, pat in RULES:
            m = pat.search(text)
            if m:
                snippet = " ".join(text[max(0, m.start() - 90):m.end() + 160].split())
                hits.append({"tag": tag, "file": path, "snippet": snippet})
        if any(h["tag"] == "forbid_agent" for h in hits if h["file"] == path):
            break

    # 工作流里有没有装识别自动化的 bot
    wf = _gh(["api", f"repos/{repo}/contents/.github/workflows"])
    bot_hits = []
    if wf:
        try:
            names = [n["name"] for n in json.loads(wf)]
        except Exception:  # noqa: BLE001
            names = []
        for n in names:
            body = _file(repo, f".github/workflows/{n}")
            low = body.lower()
            for b in SCANNER_BOTS:
                if b in low:
                    bot_hits.append(f"{n}:{b}")

    tags = sorted({h["tag"] for h in hits})
    if "forbid_agent" in tags:
        verdict = "exclude"
    elif {"assignment_gate", "bulk_automation_rejected"} & set(tags):
        verdict = "exclude"          # 会直接关 PR，进池只是浪费额度
    elif "merged_pr_threshold" in tags and not re.search(
            r"unless|except|or\s+the\s+author", " ".join(h["snippet"] for h in hits), re.I):
        verdict = "review"
    elif tags:
        verdict = "review"
    else:
        verdict = "ok"
    return {"repo": repo, "verdict": verdict, "tags": tags,
            "scanner_bots": sorted(set(bot_hits)), "hits": hits}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

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
            if res["verdict"] == "ok" and not res["scanner_bots"]:
                continue
            print(f"{res['verdict']:8s} {res['repo']:38s} {','.join(res['tags'])}")
            if res["scanner_bots"]:
                print(f"         bot: {', '.join(res['scanner_bots'])}")
            for h in res["hits"][:2]:
                print(f"         [{h['tag']}/{h['file']}] {h['snippet'][:190]}")
        ex = [r for r in results if r["verdict"] == "exclude"]
        rv = [r for r in results if r["verdict"] == "review"]
        print(f"\n{len(results)} scanned | exclude={len(ex)} review={len(rv)} ok={len(results)-len(ex)-len(rv)}")
    return 1 if any(r["verdict"] == "exclude" for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
