"""repair 模式（计划 §9）：OpenCode 产补丁 → 隔离验证证据链 → Result。

信任域（计划 §10）：
- 域 B（Agent）：在补丁工作目录里读代码、改文件；**禁止**执行 bash/联网（由写入
  work_dir 的 opencode.json 权限配置强制，若 CLI 版本不支持该键则记入 unverified）。
- 域 C（测试）：补丁后的验证在脱敏环境运行（复用 reproduce.sanitized_env）。
- 证据链（§16）：base 上测试失败 → reproduced_on_base；补丁后通过 → fixed_on_patch；
  两者缺一不可宣称 fixed。
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import tempfile
from pathlib import Path

from ..schemas import Job, Result
from .opencode_client import OpenCodeClient
from .reproduce import sanitized_env

FIX_PROMPT = """You are fixing a bug in this repository. Work autonomously.

Bug report:
{context}

Repo notes (contributed rules for this project, may be empty):
{rules}

Do this, in order:

1. EXPLORE. Read the issue above, then find the code responsible. Look at how this
   repo is built and tested (pyproject.toml / Cargo.toml / package.json /
   test dirs / CI config). Do not assume a framework.

2. WRITE A REGRESSION TEST in the repo's OWN style and location, following its
   existing conventions. It must fail on the current code for the reason described
   in the bug report. Run it. If it does not fail, your test does not capture the
   bug — fix the test, not the source.

3. FIX the bug with the smallest correct change. Re-run your test until it passes.

4. CHECK that you did not break things: run the narrowest relevant existing test
   suite for the file(s) you touched.

You have a shell. Use it: install what you need, run the test runner this project
actually uses. You may modify test files (you are adding a regression test) but do
NOT reformat unrelated code and do NOT touch CI config.

When finished, write a file named OSSCOUT_RESULT.json in the repository root:

{{"test_cmd": "<the exact single command that runs your regression test, run from the repo root>",
 "test_file": "<repo-relative path of the regression test you added>",
 "root_cause": "<one sentence>"}}

Write it as real JSON - no code fences, no comments. This file is how your work is
picked up; if it is missing or unparseable the run is discarded.
"""

# Windows 教训（P5 演练实测）：多行 prompt 经 argv 传给 opencode.CMD（批处理 shim）
# 会挂起或解析异常；Bash shell 下手动执行正常。长 prompt 写入工作目录文件，
# argv 只传单行指令。
TASK_FILE = "OSSCOUT_TASK.md"
RUN_TASK_MSG = (
    "Carry out the task in OSSCOUT_TASK.md now. Do not reply with a plan, an "
    "acknowledgement, or a progress statement - start calling tools immediately "
    "(read files, run commands, edit code) and keep working until the fix is "
    "implemented and your regression test passes. Only when you are completely "
    "finished, print the OSSCOUT_TEST_CMD / OSSCOUT_TEST_FILE / "
    "OSSCOUT_ROOT_CAUSE lines.")


def _git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", check=check)


def _run_cmd(cmd: list[str], cwd: Path, env: dict) -> tuple[int, str, str]:
    last = (1, "", "")
    for attempt in range(3):   # Windows 网络栈偶发 10106（与 TLS 抖动同源）：最多 3 次
        try:
            p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=1800)
        except FileNotFoundError as exc:
            return 127, "", str(exc)
        except subprocess.TimeoutExpired as exc:
            return 124, str(exc.stdout or ""), str(exc.stderr or "")
        if p.returncode == 0:
            return p.returncode, p.stdout, p.stderr
        last = (p.returncode, p.stdout, p.stderr)
        if "10106" not in (p.stderr or "") and "10106" not in (p.stdout or ""):
            return last
        time.sleep(8)
    return last


def enforce_domain_b(work_dir: Path) -> str:
    """写入 opencode 权限配置。

    bash 必须放开（2026-10-28 结构性修正）：修复 agent 要发现这个仓库用的是什么
    测试框架、跑自己的回归测试、看真实报错再改。禁掉 shell 等于让它闭着眼改文件，
    结果是它只能处理"我们预先猜到怎么测"的项目（实际只有 pytest 一格）。
    网络仍然禁掉：修复不需要外网，而 webfetch 会把不可信的页面内容带进上下文。
    隔离靠沙箱本身：runner/沙箱一次性、contents:read、无额外凭据。
    """
    cfg = {"permission": {"bash": "allow", "webfetch": "deny", "edit": "allow"}}
    (work_dir / "opencode.json").write_text(json.dumps(cfg), encoding="utf-8")
    return "opencode.json permission {bash:allow, webfetch:deny, edit:allow} written"


def resolve_model() -> str:
    """从 configs/model_policy.yaml 解析首选模型（fixed 模式取 primary_models[0]）。

    容器部署时源码在 /app、配置在 /data/configs（卷），因此按顺序探测：
    源码目录 → OSSCOUT_DASHBOARD_ROOT。都不存在时才报错。
    """
    import os
    import yaml
    from .model_fallback import ModelPolicy
    src_root = Path(__file__).resolve().parents[2]
    candidates = [src_root / "configs" / "model_policy.yaml"]
    data_root = Path(os.environ.get("OSSCOUT_DASHBOARD_ROOT", "/data"))
    candidates.append(data_root / "configs" / "model_policy.yaml")
    for cfg_path in candidates:
        if not cfg_path.exists():
            continue
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        models = ModelPolicy.from_yaml(cfg).models()
        if models:
            return models[0]
    raise RuntimeError(
        "no model configured (looked in: " + ", ".join(str(c) for c in candidates) + ")")


def repo_rules_block(repo: str, root: Path | None = None) -> str:
    """把该仓库的贡献规则注入 OpenCode 上下文（lane/pr_rules/notes/health 政策闸）。

    来源：configs/repositories.yaml + lane_overrides.yaml（经 PolicyEngine 合并）
    + state/health.json 的 policy flags。失败静默返回空串（不阻塞修复）。
    """
    root = root or Path(__file__).resolve().parents[2]
    try:
        from ..repo_policy import PolicyEngine
        eng, err = PolicyEngine.load(root)
        if err is not None:
            return ""
        entry = eng.repo_entry(repo)
        lane = eng.repo_lane(repo)
        lines = [f"Repository: {repo}",
                 f"Contribution lane: {lane or 'unknown'}"]
        if entry.get("pr_rules"):
            lines.append("PR rules: " + ", ".join(entry["pr_rules"]))
        if entry.get("only"):
            lines.append(f"Only consume: {entry['only']}")
        if entry.get("default_branch"):
            lines.append(f"Default branch: {entry['default_branch']}")
        hp = root / "state" / "health.json"
        if hp.exists():
            try:
                h = json.loads(hp.read_text(encoding="utf-8")).get("repos", {}).get(repo) or {}
                flags = (h.get("policy") or {}).get("flags") or []
                if flags:
                    lines.append("CONTRIBUTING policy flags: " + ", ".join(flags))
                if h.get("lane_advisory") and h["lane_advisory"] != lane:
                    lines.append(f"health lane_advisory: {h['lane_advisory']} (differs from config)")
            except (json.JSONDecodeError, OSError):
                pass
        lane_guidance = {
            "gated": "This repo requires comment-alignment + maintainer assignment "
                     "before any PR; never write 'please assign me'.",
            "restricted": "Only maintainer-confirmed candidates; PRs must include tests.",
            "observe": "No GitHub writes allowed for this repo.",
        }
        if lane in lane_guidance:
            lines.append("Lane guidance: " + lane_guidance[lane])
        if len(lines) <= 2:
            return ""
        return "\n".join(lines)
    except Exception:  # noqa: BLE001 — 规则注入失败不阻塞修复
        return ""


def _ensure_opencode_auth() -> str | None:
    """沙箱内没有 ~/.local/share/opencode/auth.json，CLI 因此无法认证。

    控制器只把 OPENCODE_API_KEY 注入到 Sandbox 进程环境（不落盘、不进 job）。
    这里在 CLI 启动前用它写一份等价的一次性 auth.json；Sandbox 结束后连同
    整个文件系统一起销毁，不留凭据。返回 provider 名（供日志），无 key 则 None。
    """
    key = (os.environ.get("OPENCODE_API_KEY") or "").strip()
    if not key:
        return None
    provider = (os.environ.get("OSSCOUT_OPENCODE_PROVIDER") or "opencode-go").strip()
    auth = Path.home() / ".local" / "share" / "opencode" / "auth.json"
    try:
        auth.parent.mkdir(parents=True, exist_ok=True)
        auth.write_text(json.dumps({provider: {"type": "api", "key": key}}),
                        encoding="utf-8")
        auth.chmod(0o600)
    except OSError:
        return None
    return provider


RESULT_FILE = "OSSCOUT_RESULT.json"


def _parse_markers(text: str) -> dict:
    """从 agent 的最终输出里取 OSSCOUT_* 标记。缺失的键不出现。"""
    out: dict = {}
    for line in (text or "").splitlines():
        s = line.strip().lstrip("> ").strip()
        for key, name in (("OSSCOUT_TEST_CMD:", "test_cmd"),
                          ("OSSCOUT_TEST_FILE:", "test_file"),
                          ("OSSCOUT_ROOT_CAUSE:", "root_cause")):
            if s.startswith(key):
                val = s[len(key):].strip().strip("`\"'")
                if val:
                    out[name] = val
    return out


def _read_result_file(work_dir: Path) -> dict:
    """读 agent 写的 OSSCOUT_RESULT.json（比解析 stdout 可靠）。

    2026-10-30：靠正则从模型输出里抓标记太脆——它会加粗、加列表符号、
    把整行包在代码块里，或者干脆忘了写。prompt 里现在要求优先落盘，
    这里优先读文件，读不到再退回正则。
    """
    import json as _json
    for name in (RESULT_FILE, ".oss-scout-result.json"):
        p = work_dir / name
        if not p.is_file():
            continue
        try:
            data = _json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        out: dict = {}
        cmd = data.get("test_cmd") or data.get("command")
        if isinstance(cmd, list):
            cmd = " ".join(str(x) for x in cmd)
        if isinstance(cmd, str) and cmd.strip():
            out["test_cmd"] = cmd.strip()
        tf = data.get("test_file") or data.get("test_path")
        if isinstance(tf, str) and tf.strip():
            out["test_file"] = tf.strip()
        rc = data.get("root_cause") or data.get("cause")
        if isinstance(rc, str) and rc.strip():
            out["root_cause"] = rc.strip()[:300]
        return out
    return {}


def _split_cmd(cmd: str) -> list[str]:
    """把 agent 报的命令行转成可执行的 argv。

    agent 常常报一整条 shell 语句，例如
    ``cd libs/partners/huggingface && python -m pytest tests/unit_tests -q``。
    直接 shlex 拆开再 exec 会让 "cd" 被当成可执行文件
    （实测 2026-10-29：base_rc=127, "[Errno 2] No such file or directory: 'cd'"）。
    含 shell 语法时交给 sh -c，其余仍按 argv 执行（保留不经 shell 的好处）。
    """
    text = (cmd or "").strip()
    if not text:
        return []
    if _needs_shell(text):
        return ["/bin/sh", "-c", text] if os.name != "nt" else ["cmd", "/c", text]
    import shlex
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


_SHELL_TOKENS = ("&&", "||", "|", ";", ">", "<", "$(", "`", "\n",
                 " 2>&1", " & ", "cd ")


def _needs_shell(text: str) -> bool:
    """这条命令需要 shell 解释吗（管道、重定向、串接、命令替换）。"""
    return any(tok in text for tok in _SHELL_TOKENS)


def opencode_patch_producer(job: Job, work_dir: Path, context: str,
                            client: OpenCodeClient | None = None,
                            model: str | None = None,
                            ai_test_paths: list[str] | None = None) -> dict:
    """让 OpenCode 在 work_dir 里自主完成"探索→写测试→修→验证"。

    返回 dict：
        patch       修复补丁（不含回归测试文件）
        test_cmd    agent 实际用过的测试命令（argv）
        test_files  {仓库相对路径: 内容}，agent 新写的回归测试
        root_cause  一句话归因
        notes       过程声明

    设计变更（2026-10-28）：以前是控制器先猜测试框架、让 API 生成测试文件、
    再叫 agent 只改源码。那样 agent 拿不到 shell、看不到真实报错，实际只能
    处理"我们预先猜到怎么测"的仓库。现在测试由 agent 自己写、自己跑——我们只在
    出口做结果验证（干净 base 上失败 → 打补丁后通过）。
    """
    ai_test_paths = list(ai_test_paths or [])
    cfg_path = work_dir / "opencode.json"
    original_cfg = cfg_path.read_bytes() if cfg_path.exists() else None
    baseline = _git(["rev-parse", "HEAD"], work_dir).stdout.strip()
    notes = [enforce_domain_b(work_dir)]
    provider = _ensure_opencode_auth()
    notes.append(f"opencode auth: {provider}" if provider
                 else "opencode auth: no OPENCODE_API_KEY in env")
    detail = ""
    try:
        # 1200s：agent 现在要装依赖、跑测试、迭代，比只改文件慢得多。
        client = client or OpenCodeClient(timeout_seconds=1200, workdir=work_dir)
        model = model or (job.model or "").strip() or resolve_model()
        rules = repo_rules_block(job.repo)
        (work_dir / TASK_FILE).write_text(
            FIX_PROMPT.format(context=context[:6000], rules=rules or "(none)"),
            encoding="utf-8")
        ok, detail = client.run(model, RUN_TASK_MSG)
        diff_early = _git(["diff"], work_dir).stdout
        # 2026-10-29 实测：模型有时只回一句"I'll start by reading the task file"
        # 就以 rc=0 退出，什么都没做（langchain#41448 那次只跑了 36 秒）。
        # 这不是失败也不是成功——重试一次，用更直接的指令逼它动手。
        if not diff_early.strip() and "I'll start" in (detail or "").lower()[:200]:
            notes.append("agent replied with a progress line and exited; retrying once")
            nudge = (RUN_TASK_MSG + " You already said you would start - actually do it "
                     "now. No narration.")
            ok, detail = client.run(model, nudge)
            diff_early = _git(["diff"], work_dir).stdout
        if not ok and not diff_early.strip():
            raise RuntimeError(f"opencode repair run failed: {detail[:300]}")
        notes.append(f"model({model}) said: {detail[:200]}")
    finally:
        if original_cfg is None:
            cfg_path.unlink(missing_ok=True)
        else:
            cfg_path.write_bytes(original_cfg)
        (work_dir / TASK_FILE).unlink(missing_ok=True)

    # 优先读 agent 落盘的结果文件，读不到再退回解析 stdout
    markers = _read_result_file(work_dir) or _parse_markers(detail)

    # agent 新增/修改过的文件（含测试）。用 --cached 才能把新文件纳入。
    _git(["add", "-A"], work_dir)
    changed = [p for p in _git(["diff", "--cached", "--name-only"], work_dir)
               .stdout.splitlines() if p.strip()]
    excludes = [p for p in ("opencode.json", TASK_FILE, RESULT_FILE,
                            ".oss-scout-result.json") if p in changed]

    # 分清"回归测试"和"修复"：
    # - agent 报的 OSSCOUT_TEST_FILE 一定是测试
    # - 其余路径里，明显新增的测试文件也归到测试（避免测试混进 PR）
    test_file = markers.get("test_file", "")
    test_files: dict[str, str] = {}
    for path in changed:
        is_test = (path == test_file) or _looks_like_test(path)
        if not is_test:
            continue
        full = work_dir / path
        if full.is_file():
            try:
                test_files[path] = full.read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass
        excludes.append(path)
    excludes.extend(ai_test_paths)

    if excludes:
        _git(["reset", "-q", "--", *excludes], work_dir, check=False)
    patch = _git(["diff", "--cached"], work_dir).stdout
    if not patch.strip():
        # 诊断信息必须带出 agent 自己说了什么、动了哪些文件——否则"没改源码"
        # 这句话无法区分"它没找到地方"和"它改了但我们的分类把它当测试剔掉了"。
        raise RuntimeError(
            "agent produced no source change "
            f"(changed={changed or 'nothing'}, tests={sorted(test_files) or 'none'}); "
            f"agent said: {(detail or '')[:400]}")

    cmd_str = markers.get("test_cmd", "").strip()
    return {
        "patch": patch,
        "test_cmd": _split_cmd(cmd_str) if cmd_str else [],
        "test_files": test_files,
        "root_cause": markers.get("root_cause", "")[:300],
        "notes": notes,
        "baseline": baseline,
        # 保留 agent 的收尾输出与它改动过的文件清单：失败时这是唯一能判断
        # "它到底理解到哪一步"的线索
        "agent_output": (detail or "")[:4000],
        "changed_files": changed,
        "markers_found": bool(markers),
    }


def _looks_like_test(path: str) -> bool:
    """路径像测试文件吗？只用于把 agent 新增的测试从 PR 补丁里剔出来。"""
    p = path.replace("\\", "/").lower()
    name = p.rsplit("/", 1)[-1]
    if "/tests/" in p or "/test/" in p or p.startswith("tests/") or p.startswith("test/"):
        return True
    if name.startswith("test_") or name.endswith("_test.py") or name.endswith("_test.go"):
        return True
    if ".test." in name or ".spec." in name or name.endswith("_test.rs"):
        return True
    return False


# 测试框架"根本没跑起来"的特征串。命中任何一个都说明退出码反映的是环境缺失，
# 而不是被测代码的行为——此时既不能声称复现，也不能声称修好。
_BROKEN_ENV_MARKERS = (
    "no module named pytest",
    "no module named 'pytest'",
    "no module named pytest'",
    "pytest: command not found",
    "pytest: not found",
    "module not found",          # node
    "cannot find module",        # node
    "command not found",
    "no such file or directory: 'pytest'",
    "is not recognized as an internal or external command",   # windows
    "error: externally-managed-environment",    # PEP 668 拦截安装
    "no testable syntax",        # go vet 级错误
    # 用例本身不存在 / 压根没收集到测试：退出码反映的是调用方式错，不是代码行为。
    # 2026-10-28 实测：base 阶段测试文件还没落盘，pytest 以 usage error(4) 退出，
    # 曾被记成 reproduced=true。
    "file or directory not found",
    "no tests ran",
    "error: not found:",
    "unrecognized arguments",
    "no such option",
)


def _test_env_broken(stdout: str, stderr: str) -> str | None:
    """判断退出码是否只是"测试环境不可用"。返回命中的特征串，未命中返回 None。"""
    text = ((stderr or "") + "\n" + (stdout or "")).lower()
    for marker in _BROKEN_ENV_MARKERS:
        if marker in text:
            return marker
    return None


def _snapshot_worktree(work: Path) -> str:
    """把 agent 的改动做成一个 patch 存着，供验证后恢复。

    只记 tracked 文件的改动；agent 装的依赖（ignored 文件）不会被 reset 影响，
    因为下面用的是 `git checkout -- .`（不动 ignored/untracked）。
    """
    _git(["add", "-A"], work, check=False)
    return _git(["diff", "--cached", "HEAD"], work, check=False).stdout


def _restore_sources_to_base(work: Path, base_sha: str) -> None:
    """只把 tracked 源码退回 base，保留 agent 装的依赖。

    不能用 `git reset --hard` + `git clean -fdx`：那会把 .venv / node_modules
    一起删掉，导致随后同一命令因为自己的环境被清空而失败（2026-10-29 实测）。
    `checkout <sha> -- .` 只改 tracked 文件的内容。
    """
    _git(["checkout", "--quiet", "--force", base_sha, "--", "."], work, check=False)


def _restore_worktree(work: Path, snapshot: str) -> None:
    """把 agent 的改动恢复回来（验证完 base 状态之后）。"""
    if not snapshot.strip():
        return
    _git(["reset", "--quiet"], work, check=False)
    subprocess.run(["git", "apply", "--whitespace=nowarn", "-"], cwd=str(work),
                   input=snapshot, capture_output=True, text=True,
                   encoding="utf-8", errors="replace", timeout=120)


def _seed_test_files(repo_dir: Path,
                     files: dict[str, str] | None) -> list[Path]:
    """把 AI 回归测试临时写进 base 树，供 base 阶段的测试运行使用。

    返回实际新建的文件列表（原本就存在的文件不覆盖、也不回删），
    由 _unseed_test_files 负责清理，避免污染工作区。
    """
    created: list[Path] = []
    for rel, content in (files or {}).items():
        target = repo_dir / rel
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        created.append(target)
    return created


def _unseed_test_files(repo_dir: Path, created: list[Path]) -> None:
    """删除 _seed_test_files 新建的文件，并清理因此变空的目录。"""
    for p in reversed(created):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            continue
        # 只在目录确实空了时向上删，避免动到仓库原有结构
        parent = p.parent
        while parent != repo_dir and parent.is_dir():
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent


def _strip_test_files_from_patch(patch: str, paths: list[str]) -> str:
    """从 unified diff 中剔除指定文件的整段改动。

    按 `diff --git a/<path> b/<path>` 分段；只保留不涉及这些路径的段。
    路径比较用纯 posix 形式，兼容 a/ b/ 前缀。
    """
    want = {p.replace("\\", "/").lstrip("./") for p in paths if p}
    if not want:
        return patch
    out: list[str] = []
    keep = True
    for line in patch.splitlines(keepends=True):
        if line.startswith("diff --git "):
            parts = line.split()
            # 形如 diff --git a/x b/x
            cand = {p[2:] for p in parts[2:4] if p[:2] in ("a/", "b/")}
            keep = not (cand & want)
        if keep:
            out.append(line)
    return "".join(out)


# 这些命令的退出码只反映"某个字符串/文件在不在"，跟被测代码的行为无关。
# 允许它们出现在复合命令里（如 `cmd && pytest`），但整条命令不能只有它们。
_NON_VERIFYING_BINARIES = {
    "grep", "egrep", "fgrep", "rg", "ag", "test", "[", "true", "false",
    "ls", "dir", "cat", "head", "tail", "wc", "stat", "find", "git", "echo",
    "printf", "sleep", "curl", "wget", "diff", "cmp",
    # 目录切换本身不验证任何行为：`cd sub && grep -q x f` 的退出码仍只反映 grep
    "cd", "export", "source", ".", "set", "pushd", "popd",
}


def _implausible_verification(cmd: list[str], patch: str,
                              test_files: dict[str, str]) -> str | None:
    """拒绝不可能真正验证这次改动的测试命令。

    只拦两种可证明无效的情形，不做风格审查：
    1) 命令行里没有任何测试运行器（全是 grep/ls/git 这类），退出码只反映文件或
       字符串是否存在；
    2) agent 没写任何回归测试，补丁却凭空"修好"了某个行为——没有测试就无从验证。
    """
    if not patch.strip():
        return "empty patch"
    if not test_files:
        return "no regression test was produced"
    import os as _os
    import shlex
    # shell 包装（["/bin/sh","-c","cd x && pytest ..."] 或 ["cmd","/c","..."]）
    # 要拆回去再判断，否则 sh 被当成 runner，grep-only 的命令就能蒙混过关。
    tokens = list(cmd)
    if len(tokens) >= 3 and _os.path.basename(tokens[0]).lower() in (
            "sh", "bash", "dash", "cmd"):
        # 第 2 个 token 是 -c 或 /c
        payload = " ".join(tokens[2:])
        try:
            tokens = shlex.split(payload)
        except ValueError:
            tokens = payload.split()
    # 只看命令的"程序名"位置：首词，以及 `&&` / `;` / `|` 之后的词。
    # 中间参数常是文件名（`grep -c foo x.py` 里 x.py 不是程序），不能算 runner。
    progs: list[str] = []
    expect_prog = True
    for token in tokens:
        if token in ("&&", "||", ";", "|"):
            expect_prog = True
            continue
        if expect_prog:
            progs.append(token)
            expect_prog = False
    names = {_os.path.basename(p).lower() for p in progs if p and not p.startswith("-")}
    runners = names - _NON_VERIFYING_BINARIES
    if not runners:
        return (f"command cannot verify code behaviour: {cmd} "
                f"(only uses {sorted(names)})")
    return None


def check_patch_content(patch: str) -> list[str]:
    """补丁内容安全检查：只允许仓库内相对路径（防 traversal/绝对路径注入）。"""
    errs = []
    for line in patch.splitlines():
        if line.startswith(("--- a/", "+++ b/")):
            path = line[6:].strip()
            if path.startswith(("/", "\\")) or ":" in path.split("/")[0] or ".." in Path(path).parts:
                errs.append(f"unsafe path in patch: {path!r}")
    return errs


def run_repair(job: Job, repo_dir: Path, out_dir: Path, *,
               test_cmd: list[str] | None = None, context: str = "",
               patch_producer=None, env_base: dict | None = None,
               extra_test_files: dict[str, str] | None = None,
               backend_name: str = "local") -> Result:
    """完整 repair 证据链。repo_dir 必须已 checkout 到 job.base_sha。

    顺序（2026-10-28 结构性调整）：
      1. 让 agent 自己探索仓库、写回归测试、跑、修、再跑。我们不再预先规定
         测试框架——那正是之前只能覆盖 pytest 项目的原因。
      2. 我们做**结果验证**：把 agent 写的测试放回干净 base，它必须失败；
         打上 agent 的修复补丁后，它必须通过。
    证据标准没有放松，只是把"怎么测"的决定权交给了 agent。

    test_cmd：可选。给了就用作兜底（agent 没报命令时）。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    unverified: list[str] = []
    # 验证环境必须和 agent 用过的**同一个**环境。
    # 2026-10-29 实测：agent 在真实 HOME 里装了 uv（报出的命令是 ~/.local/bin/uv），
    # 而验证时把 HOME 重定向到 sandbox_home，那条命令立刻 127 "not found"，
    # 于是一个完全正确的补丁被判成"未复现"。
    # 隔离的理由是"跑不可信代码"，但 agent 阶段已经在跑同一份代码了，
    # 再在验证阶段换一个干净的 HOME 只会让装好的依赖消失。
    # 仍保留：敏感变量剥离（令牌/密钥不进子进程）。
    env = sanitized_env(env_base, home=None)

    # ---- 阶段 1：agent 自主修复 ----
    patch = ""
    ai_cmd: list[str] = []
    test_files: dict[str, str] = dict(extra_test_files or {})
    root_cause = ""
    with tempfile.TemporaryDirectory() as td:
        work = Path(td) / "work"
        _git(["clone", "--quiet", str(repo_dir), str(work)], repo_dir)
        _git(["checkout", "--quiet", "--detach", job.base_sha], work, check=False)
        # 控制器若已备好测试文件（旧路径/兜底），先放进去让 agent 看得到
        for rel_path, content in (extra_test_files or {}).items():
            tf = work / rel_path
            tf.parent.mkdir(parents=True, exist_ok=True)
            tf.write_text(content, encoding="utf-8")
        try:
            if patch_producer is None:
                produced = opencode_patch_producer(
                    job, work, context,
                    ai_test_paths=list((extra_test_files or {}).keys()))
            else:
                produced = patch_producer(job, work, context)
        except RuntimeError as e:
            (out_dir / "worker.log").write_text(str(e), encoding="utf-8")
            return Result(job_id=job.job_id, backend=backend_name, outcome="failed",
                          base_sha=job.base_sha,
                          logs_path=f"artifacts/{job.job_id}/worker.log",
                          unverified_items=[str(e)[:300]])

        if isinstance(produced, dict):
            patch = produced.get("patch") or ""
            ai_cmd = list(produced.get("test_cmd") or [])
            test_files.update(produced.get("test_files") or {})
            root_cause = produced.get("root_cause") or ""
            unverified.extend(produced.get("notes") or [])
            # agent 的收尾输出单独落盘：失败时据此判断它理解到哪一步，
            # 而不是只看到"没改源码"这一句。
            agent_out = produced.get("agent_output") or ""
            if agent_out:
                (out_dir / "agent-output.txt").write_text(agent_out, encoding="utf-8")
            if produced.get("changed_files") is not None:
                (out_dir / "agent-changed.json").write_text(
                    json.dumps({"changed": produced.get("changed_files"),
                                "tests": sorted(produced.get("test_files") or {}),
                                "markers_found": produced.get("markers_found")},
                               ensure_ascii=False, indent=1), encoding="utf-8")
        else:                      # 兼容旧 producer：(patch, notes)
            patch, notes = produced
            unverified.extend(notes or [])

        cmd = ai_cmd or list(test_cmd or [])
        if not cmd:
            raise_no_cmd = ("agent did not report a test command and none was supplied; "
                            "cannot verify the fix")
            (out_dir / "worker.log").write_text(raise_no_cmd, encoding="utf-8")
            return Result(job_id=job.job_id, backend=backend_name, outcome="failed",
                          base_sha=job.base_sha,
                          logs_path=f"artifacts/{job.job_id}/worker.log",
                          unverified_items=[raise_no_cmd])

        raw_patch = patch          # 剔除测试文件之前的完整改动（供完整性检查）
        errs = check_patch_content(patch)
        if errs:
            (out_dir / "worker.log").write_text(patch[:2000], encoding="utf-8")
            return Result(job_id=job.job_id, backend=backend_name, outcome="blocked",
                          base_sha=job.base_sha,
                          logs_path=f"artifacts/{job.job_id}/worker.log",
                          unverified_items=errs)

        # 最小完整性检查（2026-10-28 审计）：结果闸只比较退出码，所以像
        # `grep -q <补丁新增的字符串> file` 这类命令能让"什么都没修"的补丁
        # 通过验证（base 失败 → 打了补丁后 grep 命中）。不是要审查 agent 的
        # 每个决定，只排除"验证根本不可能反映这个 bug"的几种形态。
        #
        # 只对真实 agent 产物生效：自定义 producer 是测试/运维注入，它自己负责
        # 证据语义（很多用例故意只给补丁不给测试文件）。
        if patch_producer is None:
            bad = _implausible_verification(cmd, raw_patch, test_files)
            if bad:
                (out_dir / "worker.log").write_text(
                    f"verification rejected: {bad}\n\ncmd: {cmd}\n"
                    f"test files: {sorted(test_files)}\n", encoding="utf-8")
                return Result(job_id=job.job_id, backend=backend_name, outcome="failed",
                              base_sha=job.base_sha,
                              logs_path=f"artifacts/{job.job_id}/worker.log",
                              unverified_items=[f"verification rejected: {bad}"])

        # 交付的补丁必须只含源码：把测试文件的整段 diff 剔掉（双重保险——
        # producer 已经按路径排除过一次，这里防自定义 producer 或未来改动漏掉）
        patch = _strip_test_files_from_patch(patch, list(test_files.keys()))

        # ---- 阶段 2：独立验证（在 agent 自己的环境里，回到 base 代码）----
        # 2026-10-29 教训：在 repo_dir 上验证是错的。agent 把依赖装在 work/
        # （.venv、~/.local/bin/uv、node_modules），repo_dir 里什么都没有，
        # 于是同一份代码在"能跑的环境"外被验证，必然 127。
        # 现在：在 work 里把源码退回 base、只保留 agent 装的依赖，再跑同一条命令。
        work_snapshot = _snapshot_worktree(work)
        _restore_sources_to_base(work, job.base_sha)
        seeded = _seed_test_files(work, test_files)
        try:
            base_rc, base_out, base_err = _run_cmd(cmd, work, env)
        finally:
            _unseed_test_files(work, seeded)
            _restore_worktree(work, work_snapshot)
        broken = _test_env_broken(base_out, base_err)
        reproduced = base_rc not in (0, 127, 9009) and broken is None

        if base_rc == 0:
            (out_dir / "worker.log").write_text(
                "the reported test passes on base; nothing to fix or the test does "
                "not capture the bug\n", encoding="utf-8")
            return Result(job_id=job.job_id, backend=backend_name, outcome="blocked",
                          base_sha=job.base_sha, reproduced_on_base=False,
                          logs_path=f"artifacts/{job.job_id}/worker.log",
                          unverified_items=["test does not fail on base — refusing to claim a fix"])
        if broken is not None:
            (out_dir / "worker.log").write_text(
                f"test command could not run on base: {broken}\n\n"
                f"--- stdout ---\n{base_out[-2000:]}\n--- stderr ---\n{base_err[-2000:]}",
                encoding="utf-8")
            return Result(job_id=job.job_id, backend=backend_name, outcome="failed",
                          base_sha=job.base_sha, reproduced_on_base=False,
                          logs_path=f"artifacts/{job.job_id}/worker.log",
                          unverified_items=[f"test environment unusable on base: {broken}"])

        # ---- 阶段 3：补丁后必须通过 ----
        # work 里已经是 agent 的改动（阶段 2 结束时已恢复），但只保留 tracked
        # 源码；测试文件单独写回，保证"验证用的测试"和"交付的补丁"分离。
        _restore_sources_to_base(work, job.base_sha)
        ap = subprocess.run(["git", "apply", "--whitespace=nowarn", "-"], cwd=work,
                            input=patch, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
        if ap.returncode != 0:
            (out_dir / "worker.log").write_text(
                "base fail evidence kept; patch apply failed:\n" + ap.stderr, encoding="utf-8")
            return Result(job_id=job.job_id, backend=backend_name, outcome="failed",
                          base_sha=job.base_sha, reproduced_on_base=reproduced,
                          logs_path=f"artifacts/{job.job_id}/worker.log",
                          unverified_items=["patch failed to apply on base"])
        # 补丁落下之后再写回归测试：补丁只含源码，测试不在里面
        for rel_path, content in test_files.items():
            tf = work / rel_path
            tf.parent.mkdir(parents=True, exist_ok=True)
            tf.write_text(content, encoding="utf-8")
        fixed_rc, out, err = _run_cmd(cmd, work, env)
        fixed = fixed_rc == 0
        if root_cause:
            unverified.append(f"agent root cause: {root_cause[:200]}")
        # 交付补丁只含源码改动；回归测试单独记录（PR 里不带我们的验证用例）
        (out_dir / "change.patch").write_text(patch, encoding="utf-8")
        (out_dir / "test-report.json").write_text(json.dumps({
            "schema_version": 1, "job_id": job.job_id, "base_sha": job.base_sha,
            "test_cmd": cmd, "base_returncode": base_rc, "patched_returncode": fixed_rc,
            # base 输出必须留存：否则"复现证据"无法审计，假阳性也无从发现
            "base_stdout_tail": base_out[-3000:], "base_stderr_tail": base_err[-3000:],
            "patched_stdout_tail": out[-3000:], "patched_stderr_tail": err[-3000:],
            "regression_test_files": sorted(test_files),
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        (out_dir / "worker.log").write_text(
            f"=== base (rc={base_rc}) ===\n{base_out[-2000:]}\n{base_err[-2000:]}\n"
            f"=== patched (rc={fixed_rc}) ===\n{out[-4000:]}\n{err[-4000:]}",
            encoding="utf-8")

    if fixed and not reproduced:
        unverified.append("patched test passed but base evidence missing — do not claim fixed")
    return Result(job_id=job.job_id, backend=backend_name,
                  outcome="succeeded" if fixed else "failed",
                  base_sha=job.base_sha,
                  reproduced_on_base=reproduced,
                  fixed_on_patch=fixed,
                  regression_tests_passed=fixed,
                  patch_path=f"artifacts/{job.job_id}/change.patch",
                  test_report_path=f"artifacts/{job.job_id}/test-report.json",
                  logs_path=f"artifacts/{job.job_id}/worker.log",
                  unverified_items=unverified if fixed else
                  unverified + ["patched test failed — fix is not verified"])
