"""PR CI 修复：已开出的 PR 上 CI 挂了且与我们的改动相关 → opencode 增量修复。

与 repair.py 的分工：
  repair   修一个上游 issue：复现 → 补丁 → 证据链 → 开新 PR。
  pr_fix   我们自己的 PR 分支上补 CI：checkout 分支 → 修 → 推回同一分支。

证据语义也不同：pr_fix 的"基线失败"就是 PR 当前挂红的检查——失败清单由
monitor_prs 在派发前抓好放进 payload，这里不再自己跑一遍复现（CI 平台已经
复现过了）。判断成功的标准是"改完之后 opencode 认为相关测试会过"，本地能跑
的测试就跑一遍验证；跑不动（环境依赖）就如实标 unverified，交回 CI 平台定夺。

由 .github/workflows/oss-scout-pr-fix.yml 在 runner 上调用。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from .opencode_client import OpenCodeClient  # noqa: E402

# 明确"与我们改动无关"的失败模式——派发层（monitor_prs）会先过滤一遍，
# 这里是第二道防线：检查名和 log 片段都要扫。只看摘录不够：status context
# （check-issue-link 这类）拉不到日志，摘录为空时名字是唯一线索
# （2026-11-02 实录：fastmcp 的 check-issue-link 就这么漏过去烧了一轮 opencode）。
EXTERNAL_PATTERNS = (
    "Vercel",                          # 授权类
    "pre-run-check",                   # vllm：需要 4+ merged PRs
    "Package Label",                   # pydantic-ai：需要 triage 权限
    "check-issue-link",                # fastmcp：等 maintainer 指派
    "DCO",                             # 签名问题应改 commit 而不是改代码
    "[gpt-live]", "gpt-live",          # 连真实 LLM 的 flaky 测试
    "test_exploration",
    "Authorize", "Authorization required",
    "CLA", "EasyCLA",
)


def build_prompt(payload: dict) -> str:
    """组装给 opencode 的修复指令。失败清单与 bot 意见都来自 payload。"""
    failing = payload.get("failing") or []
    fl = "\n".join(f"- {f.get('name', '?')}: {str(f.get('excerpt', ''))[:600]}"
                   for f in failing[:6])
    comments = payload.get("comments") or []
    cm = "\n".join(f"- [{c.get('author', '?')}] {str(c.get('body', ''))[:400]}"
                   for c in comments[:5])
    parts = [
        "CI is failing on this PR branch (this checkout IS the PR branch).",
        "Fix ONLY what these failures require. Smallest correct change.",
        "",
        "Failing checks and log excerpts:",
        fl or "- (details unavailable; run the test suite and find real failures)",
    ]
    if cm:
        parts += ["", "Recent bot comments that may require mechanical changes",
                  "(e.g. missing changelog/changeset file — adding it is part of",
                  "the fix):", cm]
    parts += [
        "",
        "Rules:",
        "- Do NOT reformat unrelated code, bump versions, or touch CI config.",
        "- If a failing check is clearly unrelated to the code in this PR",
        "  (infrastructure, auth, needs-maintainer-label), do NOT try to fix it.",
        "- Run the relevant tests if the environment allows; report what you ran.",
        "- Use --auto semantics: keep going until done, no questions.",
    ]
    return "\n".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix", required=True, help="path to fix.json payload")
    ap.add_argument("--repo-dir", required=True)
    args = ap.parse_args()

    payload = json.loads(Path(args.fix).read_text(encoding="utf-8"))
    repo_dir = Path(args.repo_dir).resolve()

    # 第二道防线：检查名和 log 片段都查。全部命中外因才跳过——混着真实
    # 测试失败时仍然修（那种情况下修代码是有意义的）。
    def _external_text() -> str:
        parts = []
        for f in payload.get("failing") or []:
            parts.append(str(f.get("name", "")))
            parts.append(str(f.get("excerpt", "")))
        return " ".join(parts)

    joined = _external_text()
    external_hits = [p for p in EXTERNAL_PATTERNS if p in joined]
    test_failure_marks = bool(__import__("re").search(
        r"FAILED|AssertionError|Traceback|error\[|panic:", joined, __import__("re").I))
    if external_hits and not test_failure_marks and not payload.get("force"):
        print(json.dumps({
            "fixed": False, "changed": 0,
            "skipped": f"external-only failures: {external_hits[:3]}",
        }))
        return 0

    from .repair import resolve_model
    model = resolve_model()
    client = OpenCodeClient(timeout_seconds=1800, workdir=repo_dir)
    prompt = build_prompt(payload)
    ok, detail = client.run(model, prompt)
    out = {"ok": ok, "output": detail}

    # 改了什么
    st = subprocess.run(["git", "status", "--porcelain"], cwd=repo_dir,
                        capture_output=True, text=True, encoding="utf-8",
                        errors="replace")
    changed = [l for l in (st.stdout or "").splitlines() if l.strip()]

    # 本地能验证就验证：找失败清单里名字像测试的，跑 pytest 节点
    test_nodes = [f.get("name", "") for f in payload.get("failing") or []
                  if "::" in f.get("name", "")]
    test_result = "not-run"
    if test_nodes and (repo_dir / "pyproject.toml").exists() or \
            test_nodes and (repo_dir / "setup.cfg").exists():
        sel = [n.split("::")[0] for n in test_nodes[:3]]
        r = subprocess.run(["python3", "-m", "pytest", "-x", "-q", *sel],
                           cwd=repo_dir, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=1500)
        test_result = f"rc={r.returncode}"
        if r.returncode != 0:
            (repo_dir / ".." / "prfix-test.log").write_text(
                (r.stdout or "")[-8000:] + (r.stderr or "")[-2000:],
                encoding="utf-8", errors="replace")

    result = {
        "fixed": bool(changed) and ok,
        "opencode_ok": ok,
        "changed_files": changed[:20],
        "test_result": test_result,
        "output_tail": str(out.get("output", ""))[-1500:],
    }
    print("##OSSCOUT_PRFIX##" + json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
