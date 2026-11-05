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
    """组装给 opencode 的修复指令。

    kind="ci"      失败清单与 bot 意见来自 payload，修到绿
    kind="review"  维护者原话是任务书，逐条落实
    """
    if (payload.get("kind") or "ci") == "review":
        author = payload.get("review_author") or "a maintainer"
        request = str(payload.get("review_comment") or "")[:4000]
        return "\n".join([
            "A maintainer reviewed this PR and asked for changes.",
            "This checkout IS the PR branch. Implement exactly what they asked.",
            "",
            f"Review from {author}:",
            request,
            "",
            "Rules:",
            "- Do what the review asks, nothing more. No opportunistic refactors.",
            "- If it points at a test that encodes the old behaviour, update that",
            "  test to the behaviour they describe.",
            "- If it asks for a new regression test, write one that would FAIL on",
            "  the pre-PR code -- a test that passes both ways proves nothing.",
            "- Match the surrounding test style in the repo.",
            "- Run the affected test package if the environment allows. If you",
            "  cannot run it, say so plainly instead of claiming success.",
            "- Do NOT reformat unrelated code, bump versions, or touch CI config.",
            "- Use --auto semantics: keep going until done, no questions.",
        ])

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


_GO_TAG_RE = None


def _detect_go_tags(repo_dir: Path) -> str:
    """从仓库自己的 CI workflow / Makefile 里找 go test 的惯用构建标签。

    不猜通用值——标签是仓库约定，猜错了照样误判。找不到就返回空串。
    """
    global _GO_TAG_RE
    if _GO_TAG_RE is None:
        import re as _re
        _GO_TAG_RE = _re.compile(r"go\s+test[^\n]*?-tags[=\s]+([A-Za-z0-9,_.-]+)")
    candidates: list[str] = []
    for pattern in (".github/workflows/*.yml", ".github/workflows/*.yaml",
                    "Makefile", "makefile"):
        for f in repo_dir.glob(pattern):
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for m in _GO_TAG_RE.finditer(text):
                tag = m.group(1).strip().strip("'\"")
                if tag and tag not in candidates:
                    candidates.append(tag)
    return candidates[0] if candidates else ""


def _run(cmd: list[str], cwd: Path, timeout: int = 1500) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except FileNotFoundError:
        return 127, f"{cmd[0]} not installed"
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {timeout}s"


def _verify(repo_dir: Path, changed: list[str],
            payload: dict) -> tuple[bool, str]:
    """跑能跑的测试，返回 (是否可信, 描述)。

    判断依据是仓库形态而非失败清单里的名字——CI 的检查名常常不带路径
    （"validate (lint)"），靠它猜测试文件只会猜错。

    返回 False 时调用方**不得推送**：一个没验证过的补丁推到别人 PR 上，
    比不推更糟。
    """
    logs = repo_dir.parent / "prfix-test.log"
    if not changed:
        return True, "no-change"

    # Go：跑改动文件所在的包。docker/compose 这类仓库只有这样才验得到。
    if (repo_dir / "go.mod").exists():
        # 统一成 posix 分隔符：go 的 import path 不认反斜杠，Windows 上
        # 直接拼 Path.parent 会得到 ./pkg\watch/，go test 会报找不到包。
        pkgs = sorted({Path(l[3:].strip()).parent.as_posix() for l in changed
                       if l.strip().endswith(".go")})
        if not pkgs:
            return True, "go: no package changed"
        targets = [f"./{p}/" if p and p != "." else "./" for p in pkgs]
        rc, out = _run(["go", "test", *targets], repo_dir)
        if rc != 0:
            # 不少仓库的测试套件要构建标签才跑得全（milvus 是
            # -tags dynamic,test；2026-11-05 实录：不带标签 rc=1，
            # 带上全绿——差点把一个真修复误杀成拒推）。从仓库自己的
            # CI/Makefile 里找惯用标签，带标签重试一次再下结论。
            tags = _detect_go_tags(repo_dir)
            if tags:
                rc2, out2 = _run(["go", "test", f"-tags={tags}", *targets],
                                 repo_dir)
                if rc2 == 0:
                    return True, f"go test -tags={tags} {targets} rc=0"
                out += "\n--- with -tags=%s ---\n" % tags + out2
            logs.write_text(out[-9000:], encoding="utf-8", errors="replace")
        return rc == 0, f"go test {targets} rc={rc}"

    # Python：优先跑失败清单里的节点，否则跑改动文件对应的测试模块
    if (repo_dir / "pyproject.toml").exists() or (repo_dir / "setup.cfg").exists() \
            or (repo_dir / "pytest.ini").exists():
        nodes = [f.get("name", "") for f in payload.get("failing") or []
                 if "::" in f.get("name", "")]
        changed_py = [l[3:].strip() for l in changed
                      if l.strip().endswith(".py")]
        # 改动的测试文件 → 跑它所在目录（和 opencode 自己验证的方式一致：
        # fastmcp#6198 实录——传源码文件 pytest rc=4 用法错误，opencode 跑
        # tests/resources 全过）。没有测试文件时退回改动源码文件。
        test_like = [f for f in changed_py
                     if Path(f).name.startswith("test_")
                     or Path(f).name.endswith("_test.py")]
        dirs = sorted({Path(f).parent.as_posix() for f in test_like})
        if dirs:
            sel = [f"./{d}/" if d and d != "." else "./" for d in dirs]
        elif nodes:
            sel = [n.split("::")[0] for n in nodes[:3]]
        else:
            sel = changed_py[:5]
        if not sel:
            return True, "python: no target"
        # fastmcp/pydantic-ai-harness 实录：依赖装在 uv 管理的 .venv 里，
        # 系统 python3 -m pytest 收集不到任何东西（rc=5）。uv.lock 存在
        # 就走 uv run（依赖已由 deps 步骤 sync 过，--no-sync 免得重新拉）。
        use_uv = (repo_dir / "uv.lock").exists()
        base = (["uv", "run", "--no-sync"] if use_uv else ["python3", "-m"])
        rc, out = _run([*base, "pytest", "-x", "-q", *sel], repo_dir)
        if rc in (4, 5):
            # pytest 的"没收集到任何测试/用法错误"：选中的源码文件本来就没有
            # 测试。改成跑仓库的测试目录（opencode 按提示加了回归测试的话
            # 通常在 tests/ 下）；没有 tests/ 才如实标记不可验证。
            tests_dir = repo_dir / "tests"
            if not tests_dir.exists():
                # 仓库结构可能是 tests 以外的名字（如 fastmcp_slim/tests）
                for cand in sorted(repo_dir.glob("**/tests/")):
                    if "node_modules" not in str(cand) and any(cand.glob("test_*.py")):
                        tests_dir = cand
                        break
            if tests_dir.exists():
                rc, out = _run([*base, "pytest", "-x", "-q", "tests/"],
                               repo_dir, timeout=1800)
                if rc == 0:
                    return True, ("pytest tests/ rc=0"
                                  + (" (uv)" if use_uv else ""))
            rc = 5
        if rc != 0:
            print("[prfix-test-tail] " + out[-1500:])
            logs.write_text(out[-9000:], encoding="utf-8", errors="replace")
        return rc == 0, f"pytest {sel} rc={rc}"

    # Node：优先对改动的测试文件跑 vitest/jest（大仓库全量套件动辄
    # 半小时，超时=未验证=拒推，promptfoo 这类仓库就卡在这）；没有
    # 目标文件或没有 runner 再回落 npm test。
    pkg = repo_dir / "package.json"
    if pkg.exists():
        import json as _json
        try:
            pkgjson = _json.loads(pkg.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pkgjson = {}
        dev = pkgjson.get("devDependencies") or {}
        dep = pkgjson.get("dependencies") or {}
        test_files = [l[3:].strip() for l in changed
                      if l.strip().endswith((".test.ts", ".test.js",
                                             ".test.tsx", ".test.mjs"))]
        runner_available = False
        for runner_cmd in (("npx", "vitest", "run", "--silent", *test_files),
                           ("npx", "jest", "--silent", *test_files)):
            runner = runner_cmd[1]
            if test_files and (runner in dev or runner in dep):
                runner_available = True
                rc, out = _run(runner_cmd, repo_dir)
                if rc != 0:
                    # 定向跑挂可能只是测试间共享状态（opencode 两文件同跑
                    # 68 全过、单文件 rc=1 的实录）；整跑一次再下结论
                    full_cmd = ([runner, "run", "--silent"]
                                if runner == "vitest" else [runner, "--silent"])
                    rcf, outf = _run(full_cmd, repo_dir, timeout=1800)
                    if rcf == 0:
                        return True, (f"{runner} full run rc=0 "
                                      f"(targeted rc={rc} was isolation)")
                    out += "\n--- full run ---\n" + outf
                    print("[prfix-test-tail] " + outf[-1500:])
                    logs.write_text(out[-9000:], encoding="utf-8",
                                    errors="replace")
                return rc == 0, f"{runner} {test_files} rc={rc}"
        scripts = pkgjson.get("scripts") or {}
        if "test" in scripts:
            rc, out = _run(["npm", "test", "--silent"], repo_dir)
            if rc != 0 and runner_available:
                # 定向跑挂可能只是测试间的共享状态问题（opencode 两文件
                # 同跑 68 全过、单文件 rc=1 的实录）；整跑一次再下结论
                full_cmd = (["npx", "vitest", "run", "--silent"]
                            if "vitest" in (dev or dep)
                            else (["npx", "jest", "--silent"]
                                  if "jest" in (dev or dep) else []))
                if full_cmd:
                    rcf, outf = _run(full_cmd, repo_dir, timeout=1800)
                    if rcf == 0:
                        return True, ("full runner rc=0 "
                                      f"(targeted rc={rc} was isolation)")
                    out += "\n--- full run ---\n" + outf
            if rc != 0:
                logs.write_text(out[-9000:], encoding="utf-8", errors="replace")
            return rc == 0, f"npm test rc={rc}"

    # 认不出怎么测：如实标记未验证，不假装通过
    return False, "unverified: no test runner detected"


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

    test_ok, test_result = _verify(repo_dir, changed, payload)

    result = {
        "fixed": bool(changed) and ok and test_ok,
        "opencode_ok": ok,
        "test_ok": test_ok,
        "changed_files": changed[:20],
        "test_result": test_result,
        "output_tail": str(out.get("output", ""))[-1500:],
    }
    print("##OSSCOUT_PRFIX##" + json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())