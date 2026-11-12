"""在 Actions runner 内完成 push + PR（2026-10-28 架构调整）。

为什么把写操作放到 runner：
VPS 到 github.com 只有一条代理线路，那几分钟不通就让整条已验证的修复作废
（实测两次 129s 超时）。runner 自带稳定的 GitHub 网络与内置 GITHUB_TOKEN，
写操作放在那里就不再有出网单点。

安全边界不变：
- 写权限只存在于**这一条 workflow**里，且仅在 job payload 显式带 publish 配置时启用；
- 修复与验证阶段仍以只读方式 clone 目标仓库，写发生在验证通过之后；
- 控制器（VPS）依旧决定是否发布——它把 publish 配置放进 payload，不放就不写。

fork 模型：对第三方仓库没有 push 权限，分支推到自己的 fork，再向原仓库开 PR。
"""
from __future__ import annotations

import json
import hashlib
import shlex
import os
import re
import subprocess
from pathlib import Path


def _ensure_fork(push_repo: str, token: str, upstream: str = "") -> str | None:
    """确保 fork 仓库存在；返回 fork 的默认分支名，失败返回 None。

    fork 已存在时直接读它的默认分支（fork 继承了上游，可能是 main 或 dev）。
    不存在就调 GitHub 的 fork API 创建，然后轮询等它可用（大仓库要几十秒）。
    """
    import time

    def _get_default() -> str | None:
        r = _run(["gh", "api", f"repos/{push_repo}", "--jq", ".default_branch"])
        if r.returncode == 0:
            name = (r.stdout or "").strip()
            return name or None
        return None

    branch = _get_default()
    if branch:
        return branch

    # 不存在 —— 用 upstream 创建 fork
    src = upstream or push_repo.split("/", 1)[-1]
    if "/" not in src:
        return None
    r = _run(["gh", "api", "-X", "POST", f"repos/{src}/forks"])
    if r.returncode != 0:
        err = (r.stderr or "").strip()
        # 已经存在（并发或之前建过）不是错误
        if "already exists" not in err.lower():
            return None

    # fork 是异步的：大仓库要几十秒才可读
    for _ in range(12):
        time.sleep(10)
        branch = _get_default()
        if branch:
            return branch
    return None


def _run(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=str(cwd) if cwd else None, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=300)


# 补丁里不允许出现的路径：构建/打包工具在验证阶段跑测试时产生的本地垃圾。
# ClickHouse#129709 实录：uv/pip 在 src/ 下生成了 UNKNOWN.egg-info，被打进
# change.patch，一路推到上游 PR，被 maintainer 点名 "Remove accidentally
# committed files"。在应用补丁之前整段丢弃这些文件的最小 diff。
_JUNK_PATH = re.compile(
    r"(^|/)(__pycache__|\.pytest_cache|\.mypy_cache|\.ruff_cache|node_modules"
    r"|\.venv|venv|dist|build|\.tox|\.coverage|coverage_html_report)(/|$)"
    r"|\.egg-info(/|$)"
    r"|\.pyc$|\.pyo$|\.log$")


def filter_patch_junk(patch_text: str) -> str:
    """丢弃补丁中指向构建垃圾路径的文件段，其余原样保留。"""
    sections = re.split(r"(?m)^(?=diff --git )", patch_text)
    kept: list[str] = []
    for sec in sections:
        if not sec.startswith("diff --git "):
            kept.append(sec)          # 开头的 diff 元信息/索引行原样保留
            continue
        m = re.match(r"diff --git a/(.+?) b/(.+?)$", sec.splitlines()[0])
        if m and (_JUNK_PATH.search(m.group(1)) or _JUNK_PATH.search(m.group(2))):
            continue                  # 垃圾路径：整段丢弃
        kept.append(sec)
    return "".join(kept)


def publish(repo_dir: Path, patch_file: Path, publish_cfg: dict) -> dict:
    """把补丁推到 fork 并向上游开 PR。返回 {ok, pr, error, branch}。

    repo_dir 是已经 checkout 在 base_sha 上的目标仓库工作树（只读阶段用的那个
    副本的独立 clone）。这里绝不在它上面直接改——另建一个分支来放补丁，避免
    污染验证阶段留下的证据。
    """
    push_repo = (publish_cfg.get("push_repo") or "").strip()
    upstream = (publish_cfg.get("upstream") or "").strip()
    branch = (publish_cfg.get("branch") or "").strip()
    base_branch = (publish_cfg.get("base_branch") or "").strip()
    title = (publish_cfg.get("title") or "").strip()
    body = (publish_cfg.get("body") or "").strip()
    if not (push_repo and upstream and branch and title):
        return {"ok": False, "error": "publish config incomplete "
                                      "(need push_repo/upstream/branch/title)"}
    assignment_rule = publish_cfg.get("assignment_rule") or {}
    if assignment_rule.get("requires_assignment"):
        from ..git_host import assignment_facts, gh_read
        issue = gh_read([f"repos/{upstream}/issues/{publish_cfg['issue_number']}"])
        if not isinstance(issue, dict) or not isinstance(issue.get("assignees"), list):
            return {"ok": False, "error": "issue assignment unavailable before publication"}
        facts = assignment_facts(issue, assignment_rule, push_repo.split("/", 1)[0])
        reason = ("Issue 已关闭" if issue.get("state") != "open"
                  else "已有指派给其他人" if facts["assigned_to_others"]
                  else "仓库要求指派给 xixi-box"
                  if not (facts["assigned_to_me"] or facts["assignment_exempt"]) else "")
        if reason:
            return {"ok": False, "error": reason, "coarse_reason": reason}
    if not patch_file.is_file() or not patch_file.read_text(encoding="utf-8").strip():
        return {"ok": False, "error": "no patch to publish"}
    expected_hash = publish_cfg.get("verified_patch_sha256")
    if expected_hash and hashlib.sha256(patch_file.read_bytes()).hexdigest() != expected_hash:
        return {"ok": False, "error": "verified delivery patch hash mismatch"}
    filtered = filter_patch_junk(patch_file.read_text(encoding="utf-8"))
    if not filtered.strip():
        return {"ok": False, "error": "no patch to publish (all sections were "
                                      "build-artifact junk)"}
    if filtered != patch_file.read_text(encoding="utf-8"):
        if expected_hash:
            return {"ok": False, "error": "verified delivery contains build artifacts; re-verification required"}
        patch_file.write_text(filtered, encoding="utf-8")
    if upstream.lower() == "prometheus/prometheus":
        area_match = re.search(r"(?m)^diff --git a/\S+ b/([\w.-]+)/", filtered)
        area = area_match[1] if area_match else "prometheus"
        description = title.removeprefix("fix: ").strip().replace("`", "'")
        title = f"{area}: {description}"
        body += f"\n\n```release-notes\n[BUGFIX] {area}: {description}\n```"

    token = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()

    # 0) 确保 fork 存在。2026-10-30 实测：langchain 和 pulumi 两次都通过了全部
    #    验证，最后 push 报 "Repository not found" —— 因为 xixi-box/<repo> 还没建。
    #    这件事必须自动化，否则每次都要人工补一步，流程就不算跑通。
    fork_note = _ensure_fork(push_repo, token, upstream)
    if fork_note is None:
        return {"ok": False,
                "error": f"fork {push_repo} is missing and could not be created "
                         f"(check the token has repo scope)"}

    # 0.5) issue 级查重：这个 issue 我们已有 open PR 就不再开第二个。
    #      2026-11-06 实录：分支名带 base 哈希（fix/<issueN>-<sha7>），每轮修复
    #      rebase 到新 base 后分支名就变了，branch 级查重拦不住——OpenViking#6823
    #      一度同时挂着三个 PR。改成查"我们在这个 issue 上的 open PR"。
    owner = push_repo.split("/")[0]
    issue_no = int(publish_cfg.get("issue_number") or 0)
    if issue_no > 0:
        listing = _run(["gh", "pr", "list", "--repo", upstream, "--author", owner,
                        "--state", "open", "--limit", "100", "--json", "number,body,url"])
        if listing.returncode != 0:
            return {"ok": False, "error": "issue-level PR lookup failed"}
        try:
            open_prs = json.loads(listing.stdout or "[]")
        except json.JSONDecodeError:
            return {"ok": False, "error": "issue-level PR lookup returned invalid JSON"}
        if len(open_prs) >= 100:
            return {"ok": False, "error": "issue-level PR lookup incomplete"}
        for p in open_prs:
            if re.search(rf"(?im)^\s*(?:fix(?:es|ed)?|clos(?:e|es|ed)|resolv(?:e|es|ed))\s+#{issue_no}\b",
                         str(p.get("body") or "")):
                return {"ok": True, "pr": str(p.get("url") or ""), "branch": branch,
                        "note": f"existing PR #{p.get('number')} for issue #{issue_no}"}

    # 1) 在验证用的仓库里建分支并应用补丁
    co = _run(["git", "checkout", "-q", "-B", branch], cwd=repo_dir)
    if co.returncode != 0:
        return {"ok": False, "error": f"checkout branch failed: {co.stderr[-200:]}"}
    with patch_file.open("r", encoding="utf-8") as f:
        ap = subprocess.run(["git", "apply", "--whitespace=nowarn", "-"], cwd=str(repo_dir),
                            stdin=f, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=120)
    if ap.returncode != 0:
        return {"ok": False, "error": f"patch apply failed: {ap.stderr[-200:]}"}
    if expected_hash:
        from .quality_gate import patch_files
        paths = patch_files(patch_file.read_text(encoding="utf-8"))
        staged = _run(["git", "add", "-A", "--", *paths], cwd=repo_dir)
        actual = _run(["git", "diff", "--cached", "--binary"], cwd=repo_dir)
        if (staged.returncode or actual.returncode
                or hashlib.sha256(actual.stdout.encode("utf-8")).hexdigest() != expected_hash):
            return {"ok": False, "error": "staged publication differs from verified patch"}
    else:
        _run(["git", "add", "-A"], cwd=repo_dir)
    # DCO：Playbook §4.3 要求查，但代码里一直没实现，导致 vllm/milvus 的 PR
    # 全部挂着 DCO 红灯（2026-10-30 实测 #64811、#64800、#54926）。
    #
    # 2026-10-31 第二次踩坑：签名用的是 "oss-scout <oss-scout@...>"，DCO bot 直接
    # 判失败——它比对的是**签名身份与 commit 作者是否一致**：
    #   Expected "wangshun <96341168+xixi-box@users.noreply.github.com>",
    #   but got "oss-scout <oss-scout@users.noreply.github.com>"
    # DCO 是法律性质的声明（"我有权提交这段代码"），署名必须是账号本人，不能是
    # 工具名。作者与签名统一用 GitHub 的 noreply 地址。
    from .commit_identity import commit_as_owner
    commit_message = [title[:72]]
    if publish_cfg.get("commit_issue_reference") and issue_no > 0:
        commit_message.append(f"Fixes: #{issue_no}")
    if upstream.lower() == "apache/rocketmq":
        tool = "Codex" if publish_cfg.get("repair_agent") == "codex" else "OpenCode"
        commit_message.append(f"Generated-by: {tool}")
    ci = commit_as_owner(repo_dir, "\n\n".join(commit_message))
    if ci.returncode != 0:
        return {"ok": False, "error": f"commit failed: {ci.stderr[-200:]}"}

    # 2) 推到 fork（跨仓库，显式给 URL；token 只在进程参数里，不落 remote 配置）
    if token:
        push_url = f"https://x-access-token:{token}@github.com/{push_repo}.git"
    else:
        push_url = f"https://github.com/{push_repo}.git"
    head = f"{push_repo.split('/')[0]}:{branch}"
    pu = _run(["git", "push", "-q", push_url, f"HEAD:refs/heads/{branch}"], cwd=repo_dir)
    if pu.returncode != 0:
        msg = (pu.stderr or "").replace(token, "***") if token else (pu.stderr or "")
        return {"ok": False, "error": f"push failed: {msg[-250:]}"}

    # 3) 向原仓库开 PR（gh 读 GH_TOKEN；runner 的 GITHUB_TOKEN 对上游有 issue 写权）
    create_args = ["gh", "pr", "create", "--repo", upstream, "--head", head,
                   "--title", title, "--body", body]
    if base_branch:
        create_args.extend(["--base", base_branch])
    pr = _run(create_args)
    if pr.returncode != 0:
        err = (pr.stderr or "").strip()
        # PR 已存在不是失败：幂等重跑时常见
        if "already exists" in err.lower():
            existing = _run(["gh", "pr", "list", "--repo", upstream, "--head", head,
                             "--json", "url", "--jq", ".[0].url"])
            url = (existing.stdout or "").strip()
            if url:
                return {"ok": True, "pr": url, "branch": branch, "note": "already existed"}
        return {"ok": False, "error": f"pr create failed: {err[-250:]}"}
    return {"ok": True, "pr": (pr.stdout or "").strip(), "branch": branch, "created": True}


def publish_from_env(job_dict: dict, repo_dir: Path, out_dir: Path) -> dict | None:
    """workflow 调用入口：payload 里没有 publish 配置时返回 None（不发布）。"""
    cfg = dict(job_dict.get("publish") or {})
    if not cfg:
        return None
    from ..roster import monitored_repo, repo_key
    upstream = cfg.get("upstream") or ""
    if not monitored_repo(upstream) or repo_key(upstream) != repo_key(job_dict.get("repo") or ""):
        return {"ok": False, "error": "repository outside monitored roster or target mismatch"}
    if repo_key(upstream) == "rclone/rclone":
        try:
            checks = json.loads((out_dir / "test-report.json").read_text(encoding="utf-8")).get("additional_checks", {})
        except (OSError, ValueError):
            checks = {}
        if checks != {"go build": 0, "make quicktest": 0}:
            return {"ok": False, "error": "rclone required build and quicktest evidence missing or failed"}
        cfg["body"] = str(cfg.get("body") or "") + "\n\nAdditional checks passed: `go build`, `make quicktest`."
    patch = out_dir / "change.patch"
    try:
        report = json.loads((out_dir / "test-report.json").read_text(encoding="utf-8"))
        result = json.loads((out_dir / "result.json").read_text(encoding="utf-8"))
        text = patch.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return {"ok": False, "error": "verified delivery metadata or patch missing"}
    from .quality_gate import patch_files, check_delivery_scope
    digest = hashlib.sha256(patch.read_bytes()).hexdigest()
    if (report.get("job_id") != job_dict.get("job_id")
            or report.get("base_sha") != job_dict.get("base_sha")
            or result.get("job_id") != job_dict.get("job_id")
            or result.get("base_sha") != job_dict.get("base_sha")
            or result.get("outcome") != "succeeded"
            or not all(result.get(key) is True for key in (
                "reproduced_on_base", "fixed_on_patch", "regression_tests_passed"))
            or report.get("base_returncode") in (None, 0)
            or report.get("patched_returncode") != 0 or report.get("failure_kind")
            or report.get("delivery_patch_sha256") != digest
            or set(report.get("delivery_files") or []) != set(patch_files(text))):
        return {"ok": False, "error": "delivery does not match verified result, lineage, or patch hash"}
    regressions = set(report.get("regression_test_files") or [])
    if not regressions or not regressions <= set(patch_files(text)):
        return {"ok": False, "error": "verified regression tests missing from delivery patch"}
    if not str(report.get("root_cause") or "").strip() or not report.get("test_cmd"):
        return {"ok": False, "error": "root cause or reproducible test command missing"}
    if check_delivery_scope(text) or filter_patch_junk(text) != text:
        return {"ok": False, "error": "delivery contains lockfile, CI, or build-artifact changes"}
    cfg["verified_patch_sha256"] = digest
    cfg["repair_agent"] = job_dict.get("repair_agent") or "opencode"
    cfg["body"] = _rewrite_body(cfg, out_dir, repair_agent=job_dict.get("repair_agent") or "opencode")
    result = publish(repo_dir, patch, cfg)
    (out_dir / "publish.json").write_text(json.dumps(result, ensure_ascii=False, indent=1),
                                          encoding="utf-8")
    print("##OSSCOUT_PUBLISH##" + json.dumps(result, ensure_ascii=False))
    return result


def _rewrite_body(cfg: dict, out_dir: Path, *, repair_agent: str = "opencode") -> str:
    """用 agent 真正查到的根因重写 PR 描述。

    控制器组 job 时还没跑出结果，只能拿 issue 标题占位，于是 PR 里出现
    "Root cause: <issue 标题>" 这种同一句话重复两遍的假分析。2026-10-30 实测
    我们开的每个 PR 都是这个样子——biome 的 CONTRIBUTING 明确说 AI 生成的低
    信号描述会被直接关掉，astral-sh 更彻底（禁止自主 agent 贡献）。
    runner 这里已经拿到 result.json，用里面 agent 写的 root_cause 覆盖掉占位，
    没有就整段删掉：宁可少一行，也不要留一句假装分析了的话。
    """
    body = str(cfg.get("body") or "")
    issue_number = cfg.get("issue_number")
    root_cause = ""
    res_file = out_dir / "test-report.json"
    report = {}
    if res_file.is_file():
        try:
            report = json.loads(res_file.read_text(encoding="utf-8"))
            root_cause = str(report.get("root_cause") or "").strip()
        except Exception:  # noqa: BLE001
            root_cause = ""
    # 只留真正的归因；与 issue 标题雷同的（就是占位串）当没有
    title = str(cfg.get("title") or "").removeprefix("fix: ").strip()
    if root_cause and root_cause.strip().lower() == title.lower():
        root_cause = ""
    lines = []
    if issue_number:
        lines.append(f"Fixes #{issue_number}")
        lines.append("")
    if root_cause:
        lines.append(f"Root cause: {root_cause[:400]}")
        lines.append("")
    if body:
        kept = [ln for ln in body.splitlines()
                if not ln.startswith(("Fixes #", "Root cause:", "AI assistance:",
                                      "Verified by running the failing test at"))]
        kept = [ln for ln in kept if ln.strip()]
        lines.extend(kept)
    upstream = str(cfg.get("upstream") or "").lower()
    if report.get("test_cmd"):
        command = shlex.join(report["test_cmd"]).replace("`", "\\`")
        lines.extend(["", f"Tests: `{command}` (fails on base; passes with this patch)."])
    if report.get("regression_test_files"):
        lines.append("Regression coverage: " + ", ".join(
            f"`{path}`" for path in report["regression_test_files"]) + ".")
    agent = {"opencode": "OpenCode", "codex": "Codex"}.get(repair_agent, "a coding agent")
    lines.extend(["", f"AI assistance: {agent} helped implement and test this change. "
                  "oss-scout independently replayed the regression before opening this PR."])
    if upstream == "qdrant/qdrant" and issue_number:
        lines.extend(["", f"Repair task: reproduce the bug in #{issue_number}, implement a focused fix, "
                      "and verify that the regression test fails on dev and passes with the patch."])
    return "\n".join(lines).strip() or body
