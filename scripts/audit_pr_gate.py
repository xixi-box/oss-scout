"""核查仓库对未受邀/未指派 PR 的实际处理方式。

    python scripts/audit_pr_gate.py

判据来自 workflow 的**实际内容**，不是文件名：
- close / github-script 里调 pulls.update(state=closed)
- 出现 "require-issue-link" / "not assigned" / "missing-issue-link" 这类标签或文案
- 只在 PR 上留言（actions-comment-pull-request、createComment）→ 不算门禁

输出每个仓库一行，并给出 requires_invite 的建议值。
"""
from __future__ import annotations

import base64
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 强信号：真的会关闭 PR
CLOSE_PATTERNS = [
    r"state\s*:\s*['\"]?closed",
    r"pulls\.update\([^)]*state",
    r"issues\.update\([^)]*state",
    r"\bclosePullRequest\b",
    r"missing-issue-link",
    r"require-issue-link",
    r"--close\b",
]
# 强信号：要求先指派/先受邀
ASSIGN_PATTERNS = [
    r"not assigned to the linked issue",
    r"assignee",
    r"please assign",
    r"awaiting.?assignment",
    r"needs.?assignee",
]


def gh(args: list[str], timeout: int = 60) -> tuple[int, str]:
    p = subprocess.run(["gh"] + args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    return p.returncode, (p.stdout or "")


def list_workflows(repo: str) -> list[str]:
    rc, out = gh(["api", f"repos/{repo}/contents/.github/workflows",
                  "--jq", "[.[] | select(.type==\"file\") | .name]"])
    if rc != 0:
        return []
    try:
        return json.loads(out or "[]")
    except json.JSONDecodeError:
        return []


def read_workflow(repo: str, name: str) -> str:
    rc, out = gh(["api", f"repos/{repo}/contents/.github/workflows/{name}",
                  "--jq", ".content"])
    if rc != 0:
        return ""
    try:
        return base64.b64decode(out.strip()).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""


def classify(repo: str) -> dict:
    names = list_workflows(repo)
    closers: list[str] = []
    assigners: list[str] = []
    for n in names:
        low = n.lower()
        # 只看 PR 相关的 workflow，省时间
        if not any(k in low for k in ("pr", "pull", "assign", "triage",
                                      "issue", "welcome", "first")):
            continue
        body = read_workflow(repo, n)
        if not body:
            continue
        if any(re.search(p, body, re.I) for p in CLOSE_PATTERNS):
            closers.append(n)
        if any(re.search(p, body, re.I) for p in ASSIGN_PATTERNS):
            assigners.append(n)
    return {
        "repo": repo,
        "workflows": len(names),
        "closers": closers,
        "assigners": assigners,
        # 只有"会关闭"才是必须先进门禁的硬理由
        "requires_invite": bool(closers),
    }


def main() -> int:
    src = ROOT / "state" / "gate_candidates.txt"
    repos = [l.strip() for l in src.read_text(encoding="utf-8").splitlines()
             if l.strip()]
    seen, uniq = set(), []
    for r in repos:
        if r not in seen:
            seen.add(r)
            uniq.append(r)

    out = []
    for i, repo in enumerate(uniq, 1):
        rec = classify(repo)
        out.append(rec)
        verdict = "GATED" if rec["requires_invite"] else ("ASSIGN?" if rec["assigners"] else "open")
        print(f"[{i}/{len(uniq)}] {verdict:8s} {repo}", flush=True)
        if rec["closers"]:
            print(f"          closer: {', '.join(rec['closers'][:3])}")
        elif rec["assigners"]:
            print(f"          assign-ish: {', '.join(rec['assigners'][:3])}")

    dest = ROOT / "state" / "pr_gate_audit.json"
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    gated = [r for r in out if r["requires_invite"]]
    print(f"\n确认为硬门禁 {len(gated)} / {len(out)}", file=sys.stderr)
    print(f"saved: {dest}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
