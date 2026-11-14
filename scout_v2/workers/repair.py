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
import hashlib
import re
import os
import subprocess
import time
import tempfile
from contextlib import ExitStack
from pathlib import Path

from ..schemas import Job, Result
from .opencode_client import OpenCodeClient
from .reproduce import sanitized_env


class _RepairWorkspace(tempfile.TemporaryDirectory):
    """Cleanup failures must never discard the repair result or its evidence."""

    def __init__(self, out_dir: Path):
        self.out_dir = out_dir
        super().__init__(ignore_cleanup_errors=True)

    def cleanup(self):
        error = ""
        try:
            super().cleanup()
        except OSError as exc:
            error = type(exc).__name__
        if error or Path(self.name).exists():
            with (self.out_dir / "cleanup.log").open("a", encoding="utf-8") as log:
                log.write(f"Temporary workspace cleanup incomplete ({error or 'directory remains'}); "
                          "repair result retained; runner teardown will release it.\n")

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

   Before changing any behaviour, run the FULL test suite of the package you are
   about to modify, not just your own new test. Existing tests often encode the
   current behaviour as a contract, and a change that looks correct can contradict
   one. If an existing test asserts the behaviour you are changing, STOP and
   reconsider: either the fix belongs at a different layer, or that test is part of
   what this change is meant to update. Report which one in "root_cause".

   Read every comment on the issue before choosing a fix. If someone -- a
   maintainer or a bot -- has already diagnosed it, follow that diagnosis or pick a
   different issue; do not spend a cycle re-deriving a worse version of it.

You have a shell. Use it: install what you need, run the test runner this project
actually uses. You may modify test files (you are adding a regression test) but do
NOT reformat unrelated code and do NOT touch CI config.

As soon as you identify the regression command, write OSSCOUT_RESULT.json in the
repository root. Complete it using the output of the actual base and patched runs:

{{"setup_steps": [{{"kind": "setup", "command": "<install dependencies or prepare fixtures>"}},
                  {{"kind": "build", "command": "<build from the currently checked-out sources>"}}],
 "test_cmd": "<the exact command that runs the same regression on both versions>",
 "test_file": "<repo-relative path of the regression test you added>",
 "base_failure": ["<literal test name present in both runs>", "<literal assertion or wrong result for this bug>"],
 "patched_success": ["<literal output showing the targeted test ran successfully>"],
 "root_cause": "<one sentence>"}}

Use an empty setup_steps list only if the test runs directly from a fresh checkout.
The verifier runs the same steps in two fresh source trees from the repository
root. Each command runs in its own process. Include any cd or environment
assignments in the command that uses them. Build and test the checked-out source,
never reuse a binary from another checkout. If a service must run during the
test, start it from the current checkout and stop it in the same command.
Do not use source-text assertions, mock the behavior under test, or print the
evidence strings yourself unless they report an actual checked result. Use
short, stable excerpts from the test output without paths, timings, or colors.
Enable verbose output if needed so the same case name appears in both runs.
Write real JSON. A missing or incomplete verification plan blocks publication.
For Rust inline tests, keep test edits inside the existing trailing #[cfg(test)]
module so the verifier can apply the test without applying the production fix.
The regression tests will be delivered in the PR with the fix. Keep them in the
project's normal test locations and run the affected existing tests too.
Before finishing, inspect the final diff, CONTRIBUTING rules and the repository's
PR template (.github/PULL_REQUEST_TEMPLATE.md, .github/pull_request_template.md,
or the applicable template in .github/PULL_REQUEST_TEMPLATE/).
Add pr_title and pr_body to OSSCOUT_RESULT.json. Write a concise English change
title, not a copy of the bug report's title. In pr_body explain the observed
problem and root cause, the concrete fix and relevant compatibility implications.
Follow the applicable PR template, preserving its headings and meaningful fields.
Use exactly one {{VERIFIED_TESTS}} placeholder for all test commands and test
results: the publisher replaces it with independently verified evidence. Do not
invent checks, mark unverified checklist items as complete, or claim CI is green.
Do not add generic AI-assistance or oss-scout promotional text. If a repository
requires a disclosure or attestation that cannot be satisfied, report the blocker
instead of claiming it was satisfied. Keep the description focused on this diff.
Restore incidental dependency-installation changes before finishing. Do not
change lockfiles or CI workflows for these automatic bug fixes. If the task
requires dependency or CI changes, report that blocker instead of broadening it.
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


def _verify_command(cmd, work, env, out_dir, stage, *, rebuild=False):
    commands = [["make", "-j", "2"], cmd] if rebuild else [cmd]
    with (out_dir / "verification-steps.jsonl").open("a", encoding="utf-8") as log_file:
        for index, command in enumerate(commands):
            started = time.time()
            rc, stdout, stderr = _run_cmd(command, work, env)
            stdout, stderr = _mask_opencode_key(stdout), _mask_opencode_key(stderr)
            logged_command = [_mask_opencode_key(arg) for arg in command]
            log_file.write(json.dumps({
                "stage": stage, "command": logged_command, "started_at_epoch": started,
                "elapsed_seconds": round(time.time() - started, 2), "returncode": rc,
            }, ensure_ascii=False) + "\n")
            log_file.flush()
            with (out_dir / "verification.log").open("a", encoding="utf-8") as output:
                output.write(f"=== {stage}: {json.dumps(logged_command, ensure_ascii=False)} (rc={rc}) ===\n"
                             f"--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}\n")
            if rebuild and index == 0 and rc:
                return rc, stdout, f"verification rebuild failed\n{stderr}"
    return rc, stdout, stderr


def _mask_opencode_key(text: str) -> str:
    key = os.environ.get("OPENCODE_API_KEY")
    return text.replace(key, "[REDACTED]") if key else text


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
            data = _json.loads(_mask_opencode_key(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        out: dict = {}
        for key, limit in (("pr_title", 240), ("pr_body", 16000)):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                out[key] = value.strip()[:limit]
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
        for key in ("setup_steps", "base_failure", "patched_success"):
            if key in data:
                out[key] = data[key]
        return out
    return {}


def _normalise_test_target(cmd: list[str]) -> list[str]:
    """去掉 pytest 的 `::test_name` 后缀，只按文件跑。

    2026-10-30 实测：agent 报的命令是
      pytest -q .../test_x.py::test_from_model_id_accepts_device_string...
    而它在 base 阶段把测试文件种进树里之后，pytest 报
      ERROR: not found: ...::test_... (no match in any of [<Module ...>])
    ——给单个用例名做验证太脆（用例名要完全一致才成立）。按整个文件跑，
    只要该文件里有用例在 base 上失败就足以构成复现证据。
    注意：workspace/路径里不会有 `::`，所以这个替换是安全的。
    """
    out: list[str] = []
    for token in cmd:
        if "::" in token and not token.startswith("-"):
            out.append(token.split("::", 1)[0])
        else:
            out.append(token)
    return out


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
_ENV_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")


def _needs_shell(text: str) -> bool:
    """这条命令需要 shell 解释吗（管道、重定向、串接、命令替换）。"""
    return bool(_ENV_ASSIGNMENT.match(text)) or any(
        tok in text for tok in _SHELL_TOKENS)


def opencode_patch_producer(job: Job, work_dir: Path, context: str,
                            client: OpenCodeClient | None = None,
                            model: str | None = None,
                            ai_test_paths: list[str] | None = None,
                            diagnostics_dir: Path | None = None) -> dict:
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
    if job.repair_agent not in ("opencode", "codex"):
        raise RuntimeError("Unknown repair agent")
    notes = []
    if job.repair_agent == "opencode":
        notes.append(enforce_domain_b(work_dir))
        provider = _ensure_opencode_auth()
        notes.append(f"opencode auth: {provider}" if provider
                     else "opencode auth: no OPENCODE_API_KEY in env")
    detail = ""
    try:
        # 1200s：agent 现在要装依赖、跑测试、迭代，比只改文件慢得多。
        # 2400s (40 分钟)：pulumi 这类 Go 大仓库，agent 先要摸清 workspace 布局
        # 再装工具链、跑 go test。2026-10-30 实测 1200s 正好在"还没开始改"时被
        # 掐断。workflow 本身有 90 分钟预算，这里给 40 分钟留给 agent，剩下
        # 50 分钟够两轮测试跑完。
        if client is None:
            if job.repair_agent == "codex":
                from .codex_client import CodexClient
                client = CodexClient(timeout_seconds=2400, workdir=work_dir,
                                     diagnostics_dir=diagnostics_dir)
            else:
                client = OpenCodeClient(timeout_seconds=2400, workdir=work_dir)
        model = model or (job.model or "").strip()
        if not model and job.repair_agent == "opencode":
            model = resolve_model()
        rules = repo_rules_block(job.repo)
        (work_dir / TASK_FILE).write_text(
            FIX_PROMPT.format(context=context, rules=rules or "(none)"),
            encoding="utf-8")
        ok, detail = client.run(model, RUN_TASK_MSG)
        detail = _mask_opencode_key(detail or "")
        diff_early = _git(["diff"], work_dir).stdout
        # 2026-10-29 实测：模型有时只回一句"I'll start by reading the task file"
        # 就以 rc=0 退出，什么都没做（langchain#41448 那次只跑了 36 秒）。
        # 这不是失败也不是成功——重试一次，用更直接的指令逼它动手。
        if not diff_early.strip() and "I'll start" in (detail or "").lower()[:200]:
            notes.append("agent replied with a progress line and exited; retrying once")
            nudge = (RUN_TASK_MSG + " You already said you would start - actually do it "
                     "now. No narration.")
            ok, detail = client.run(model, nudge)
            detail = _mask_opencode_key(detail or "")
            diff_early = _git(["diff"], work_dir).stdout
        if not ok and not diff_early.strip():
            raise RuntimeError(f"{job.repair_agent} repair run failed: {detail[:300]}")
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

    test_file = markers.get("test_file", "")
    from .publish_remote import _JUNK_PATH, filter_patch_junk
    test_files: dict[str, str] = {}
    mixed_tests: dict[str, tuple[str, str]] = {}
    for path in changed:
        if _JUNK_PATH.search(path):
            excludes.append(path)
            continue
        is_test = (path == test_file) or _looks_like_test(path)
        if not is_test:
            continue
        full = work_dir / path
        if path == test_file and path.endswith(".rs") and not _looks_like_test(path) and full.is_file():
            old = _git(["show", f"{baseline}:{path}"], work_dir, check=False)
            current = full.read_text(encoding="utf-8")
            baseline_parts = _rust_inline_tests(old.stdout) if old.returncode == 0 else None
            current_parts = _rust_inline_tests(current)
            if not baseline_parts or not current_parts or baseline_parts[1] == current_parts[1]:
                raise RuntimeError(f"cannot separate inline regression test from Rust source: {path}")
            base_source, base_tests = baseline_parts
            new_source, new_tests = current_parts
            original = full.read_bytes()
            try:
                full.write_bytes((new_source + base_tests).encode("utf-8"))
                _git(["add", "--", path], work_dir)
            finally:
                full.write_bytes(original)
            mixed_tests[path] = (base_source + new_tests, current)
            continue
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
    # Verify the same source patch that the publisher will actually apply.
    patch = filter_patch_junk(patch)
    if not patch.strip():
        # 诊断信息必须带出 agent 自己说了什么、动了哪些文件——否则"没改源码"
        # 这句话无法区分"它没找到地方"和"它改了但我们的分类把它当测试剔掉了"。
        raise RuntimeError(
            "agent produced no source change "
            f"(changed={changed or 'nothing'}, tests={sorted(test_files) or 'none'}); "
            f"agent said: {(detail or '')[:400]}")

    cmd_str = markers.get("test_cmd", "").strip()
    has_plan = any(key in markers for key in ("setup_steps", "base_failure", "patched_success"))
    return {
        "patch": patch,
        "test_cmd": _split_cmd(cmd_str) if has_plan else _normalise_test_target(_split_cmd(cmd_str)),
        "verification_plan": {key: markers.get(key) for key in
                              ("setup_steps", "base_failure", "patched_success")} if has_plan else None,
        "test_files": test_files,
        "mixed_tests": mixed_tests,
        "root_cause": markers.get("root_cause", "")[:300],
        "pr_title": markers.get("pr_title", ""),
        "pr_body": markers.get("pr_body", ""),
        "notes": notes,
        "baseline": baseline,
        # 保留 agent 的收尾输出与它改动过的文件清单：失败时这是唯一能判断
        # "它到底理解到哪一步"的线索
        "agent_output": (detail or "")[:4000],
        "agent_failure": (detail or "")[:400] if not ok else "",
        "changed_files": changed,
        "markers_found": bool(markers),
    }


def _rust_inline_tests(text: str) -> tuple[str, str] | None:
    """Split a conventional trailing, top-level cfg(test) module."""
    matches = list(re.finditer(r"(?m)^#\[cfg\(test\)\][ \t]*\nmod tests[ \t]*\{[^\n]*\n", text))
    if len(matches) != 1:
        return None
    start = matches[0].start()
    # Reject code following the first top-level closing brace. The split only
    # supports the conventional trailing module, with indented nested blocks.
    if not re.fullmatch(r"[\s\S]*?\n\}[ \t]*\s*", text[start:]) or re.search(
            r"(?m)^\}[ \t]*\n\s*\S", text[start:]):
        return None
    return text[:start], text[start:]


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
    "verification rebuild failed",
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
    "no tests were found",
    "collected 0 items",
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


def _verification_error(rc: int, stdout: str, stderr: str, kind: str = "test") -> tuple[str, str] | None:
    if not rc and kind != "test":
        return None
    text = f"{stdout}\n{stderr}".lower()
    if rc == 124:
        return "timeout", "verification timed out"
    missing = re.search(
        r"no module named [^\n]+|cannot find module [^\n]+|[^\n]+was not found in path"
        r"|fatal error: [^\n]+no such file or directory|could not find a package configuration file[^\n]*",
        text)
    if missing:
        return "missing_dependency", missing.group().strip()[:180]
    if rc in (126, 127, 9009) or (rc == 2 and "syntax error" in text):
        return "command_error", f"command could not run (exit {rc})"
    if "verification rebuild failed" in text or (rc and (
        kind == "build" or re.search(
            r"\[build failed\]|error\[e\d+\]|fatal error:|(?:^|\n)[^\n]+:\d+(?::\d+)?: error:"
            r"|error ts\d+:|compilation failed|could not compile", text)
    )):
        return "compile_failed", "build or test compilation failed"
    broken = _test_env_broken(stdout, stderr)
    if broken:
        return "command_error", broken
    if rc and kind == "setup":
        return "setup_failed", f"environment preparation failed (exit {rc})"
    return None


def _verification_plan_valid(plan: dict) -> bool:
    steps = plan.get("setup_steps")
    if not isinstance(steps, list) or not all(
        isinstance(step, dict) and step.get("kind") in ("setup", "build")
        and isinstance(step.get("command"), str) and step["command"].strip()
        for step in steps
    ):
        return False
    for key, minimum in (("base_failure", 2), ("patched_success", 1)):
        markers = plan.get(key)
        if (not isinstance(markers, list) or not all(isinstance(marker, str) and marker.strip()
                                                     for marker in markers)
                or len({marker.strip() for marker in markers}) < minimum):
            return False
    return True


def _test_case_identity(marker: str) -> str:
    """A runner's FAIL/ok suffix changes while the case identity stays fixed."""
    marker = re.sub(r"^(?:FAILED|FAIL|ERROR|PASSED|PASS)[: ]+", "", marker.strip())
    return re.sub(r"\s+(?:\.{2,}\s*)?(?:FAILED|FAIL|ERROR|PASSED|PASS|OK|ok)$", "", marker)


def _replay_regression(cmd, plan, work, env, out_dir, stage, *, rebuild=False):
    if plan and plan["setup_steps"]:
        # Include new source and test files from the patch in the source guard.
        _git(["add", "-N", "--", "."], work)
        source = _git(["diff", "HEAD", "--binary"], work).stdout
        for index, step in enumerate(plan["setup_steps"], 1):
            kind = step["kind"]
            rc, stdout, stderr = _verify_command(
                _split_cmd(step["command"]), work, env, out_dir, f"{stage}-{kind}-{index}")
            if error := _verification_error(rc, stdout, stderr, kind):
                return rc, stdout, stderr, error
        if source != _git(["diff", "HEAD", "--binary"], work).stdout:
            return 1, "", "", ("setup_changed_sources", "setup modified tracked source or tests")
    rc, stdout, stderr = _verify_command(cmd, work, env, out_dir, stage, rebuild=rebuild)
    return rc, stdout, stderr, _verification_error(rc, stdout, stderr)


def _write_base_diagnostics(out_dir: Path, job: Job, cmd: list[str], rc: int,
                            stdout: str, stderr: str,
                            test_files: dict, patch: str, *, failure_kind: str, plan: dict | None) -> None:
    """base 阶段中止时也要留下可诊断的证据。

    2026-10-30：blocked 分支原样返回，test-report 不生成，于是"测试在 base 上
    没失败"这句话没有上下文——看不到跑的什么命令、agent 的测试长什么样、
    那条命令输出了什么。
    """
    try:
        (out_dir / "test-report.json").write_text(json.dumps({
            "schema_version": 1, "job_id": job.job_id, "base_sha": job.base_sha,
            "stage": "base", "test_cmd": cmd, "base_returncode": rc,
            "failure_kind": failure_kind, "verification_plan": plan,
            "base_stdout_tail": stdout[-4000:], "base_stderr_tail": stderr[-4000:],
            "regression_test_files": sorted(test_files),
            "patch_bytes": len(patch or ""),
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        for rel, content in (test_files or {}).items():
            safe = Path(rel).name
            (out_dir / f"agent-test-{safe}").write_text(content, encoding="utf-8")
    except OSError:
        pass


def _restore_sources_to_base(work: Path, base_sha: str) -> None:
    """Restore the index and sources, including removal of newly staged files."""
    _git(["restore", f"--source={base_sha}", "--staged", "--worktree", "--", "."], work)


def _checkout_repair_tree(repo_dir: Path, work: Path, base_sha: str) -> None:
    _git(["clone", "--quiet", str(repo_dir), str(work)], repo_dir)
    _git(["checkout", "--quiet", "--detach", base_sha], work)


def _seed_test_files(repo_dir: Path,
                     files: dict[str, str] | None) -> list[tuple[Path, str | None]]:
    """把 agent 的测试写进 base 树，供 base 阶段的运行使用。

    返回 [(path, 原内容或 None)]：None 表示这个文件是我们新建的（清理时删除），
    有内容表示它原本就存在（清理时写回）。

    必须覆盖已存在的文件（2026-10-30 实测）：agent 常常在仓库**已有**的测试
    文件里追加用例，而 base 版本没有那个用例。早期实现"文件已存在就跳过"，
    结果 base 阶段跑 `pytest path::test_new_case` 直接 "no match" —— 看起来像
    环境坏了，其实是我们的种子逻辑把测试丢了。
    """
    created: list[tuple[Path, str | None]] = []
    for rel, content in (files or {}).items():
        target = repo_dir / rel
        original: str | None = None
        if target.exists():
            try:
                original = target.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        created.append((target, original))
    return created


def _unseed_test_files(repo_dir: Path,
                       created: list[tuple[Path, str | None]]) -> None:
    """恢复 _seed_test_files 动过的文件：新建的删除，原有的写回。"""
    for p, original in reversed(created):
        try:
            if original is None:
                p.unlink(missing_ok=True)
            else:
                p.write_text(original, encoding="utf-8")
                continue
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
    "cd", "export", "source", ".", "set", "pushd", "popd", "mkdir",
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
            lexer = shlex.shlex(payload, posix=True, punctuation_chars=";&|")
            lexer.whitespace_split = True
            tokens = list(lexer)
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
            if _ENV_ASSIGNMENT.match(token) or token == "env":
                continue
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
    plan = None
    test_files: dict[str, str] = dict(extra_test_files or {})
    mixed_tests: dict[str, tuple[str, str]] = {}
    root_cause = ""
    agent_out = ""
    pr_title, pr_body = "", ""
    with ExitStack() as workspace:
        agent_dir = _RepairWorkspace(out_dir)
        work = Path(workspace.enter_context(agent_dir)) / "work"
        _checkout_repair_tree(repo_dir, work, job.base_sha)
        # 控制器若已备好测试文件（旧路径/兜底），先放进去让 agent 看得到
        for rel_path, content in (extra_test_files or {}).items():
            tf = work / rel_path
            tf.parent.mkdir(parents=True, exist_ok=True)
            tf.write_text(content, encoding="utf-8")
        try:
            if patch_producer is None:
                produced = opencode_patch_producer(
                    job, work, context,
                    ai_test_paths=list((extra_test_files or {}).keys()), diagnostics_dir=out_dir)
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
            plan = produced.get("verification_plan")
            test_files.update(produced.get("test_files") or {})
            mixed_tests = produced.get("mixed_tests") or {}
            root_cause = produced.get("root_cause") or ""
            pr_title = produced.get("pr_title") or ""
            pr_body = produced.get("pr_body") or ""
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
            # 说清楚是哪一种失败。2026-10-30 实测：pulumi 那次 agent 超时且零改动，
            # 却只报"没报测试命令"，读起来像它只忘了写结果文件，实际是根本没干活。
            if not (patch or "").strip():
                raise_no_cmd = (
                    "agent produced no changes and no test command - it likely ran out "
                    f"of time or never located the code; agent said: {(agent_out or '')[:300]}")
            else:
                agent_failure = produced.get("agent_failure") if isinstance(produced, dict) else ""
                raise_no_cmd = ("agent changed files but did not report a test command, "
                                "so the change cannot be verified")
                if agent_failure:
                    raise_no_cmd = f"{agent_failure[:210]} · {raise_no_cmd}"
            (out_dir / "worker.log").write_text(raise_no_cmd, encoding="utf-8")
            return Result(job_id=job.job_id, backend=backend_name, outcome="failed",
                          base_sha=job.base_sha,
                          logs_path=f"artifacts/{job.job_id}/worker.log",
                          unverified_items=[raise_no_cmd])

        raw_patch = patch          # 剔除测试文件之前的完整改动（供完整性检查）
        if (patch_producer is None or plan is not None) and not (
            isinstance(plan, dict) and _verification_plan_valid(plan)
        ):
            reason = "verification_plan_invalid: missing setup steps or observed test evidence"
            (out_dir / "worker.log").write_text(reason, encoding="utf-8")
            return Result(job_id=job.job_id, backend=backend_name, outcome="blocked",
                          base_sha=job.base_sha, logs_path=f"artifacts/{job.job_id}/worker.log",
                          unverified_items=[reason])
        from .quality_gate import check_delivery_scope, patch_files, check_patch_path
        errs = check_patch_content(patch) + check_delivery_scope(patch)
        for path in test_files | mixed_tests:
            errs.extend(check_patch_path(path))
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
        if patch_producer is None or plan is not None:
            bad = _implausible_verification(cmd, raw_patch, test_files | mixed_tests)
            if bad:
                (out_dir / "worker.log").write_text(
                    f"verification rejected: {bad}\n\ncmd: {cmd}\n"
                    f"test files: {sorted(test_files)}\n", encoding="utf-8")
                return Result(job_id=job.job_id, backend=backend_name, outcome="failed",
                              base_sha=job.base_sha,
                              logs_path=f"artifacts/{job.job_id}/worker.log",
                              unverified_items=[f"verification rejected: {bad}"])

        # Base verification needs test-only changes separated from the fix.
        # Reassemble them into the exact deliverable before patched verification.
        patch = _strip_test_files_from_patch(patch, list(test_files.keys()))
        rebuild = (plan is None and job.repo.lower() == "python/cpython" and cmd[0] == "./python"
                   and bool(re.search(r"(?m)^diff --git a/.+\.[ch] b/.+\.[ch]$", patch)))

        # Legacy injected producers can still reuse the agent's work tree.
        # 2026-10-29 教训：在 repo_dir 上验证是错的。agent 把依赖装在 work/
        # （.venv、~/.local/bin/uv、node_modules），repo_dir 里什么都没有，
        # 于是同一份代码在"能跑的环境"外被验证，必然 127。
        # 现在：在 work 里把源码退回 base、只保留 agent 装的依赖，再跑同一条命令。
        if plan is not None:
            agent_dir.cleanup()
            base_dir = _RepairWorkspace(out_dir)
            work = Path(workspace.enter_context(base_dir)) / "verify-base"
            _checkout_repair_tree(repo_dir, work, job.base_sha)
        else:
            _restore_sources_to_base(work, job.base_sha)
        baseline_tests = test_files | {path: versions[0] for path, versions in mixed_tests.items()}
        seeded = _seed_test_files(work, baseline_tests)
        try:
            base_rc, base_out, base_err, error = _replay_regression(
                cmd, plan, work, env, out_dir, "base", rebuild=rebuild)
        finally:
            _unseed_test_files(work, seeded)
        if error is None and base_rc == 0:
            error = "base_test_passed", "test does not fail on base"
        if error is None and plan and not all(marker.strip() in f"{base_out}\n{base_err}"
                                             for marker in plan["base_failure"]):
            error = "base_failure_unconfirmed", "target test and bug symptom not found in base output"
        if error:
            kind, detail = error
            reason = f"{kind} on base: {detail}"
            (out_dir / "worker.log").write_text(
                f"{reason}\n\n"
                f"--- stdout ---\n{base_out[-2000:]}\n--- stderr ---\n{base_err[-2000:]}",
                encoding="utf-8")
            _write_base_diagnostics(out_dir, job, cmd, base_rc, base_out, base_err,
                                    baseline_tests, raw_patch, failure_kind=kind, plan=plan)
            return Result(job_id=job.job_id, backend=backend_name,
                          outcome="blocked" if kind == "base_test_passed" else "failed",
                          base_sha=job.base_sha, test_report_path=f"artifacts/{job.job_id}/test-report.json",
                          logs_path=f"artifacts/{job.job_id}/worker.log",
                          unverified_items=[reason])

        # ---- 阶段 3：补丁后必须通过 ----
        if plan is not None:
            base_dir.cleanup()
            work = Path(workspace.enter_context(_RepairWorkspace(out_dir))) / "verify-patched"
            _checkout_repair_tree(repo_dir, work, job.base_sha)
        else:
            _restore_sources_to_base(work, job.base_sha)
        ap = subprocess.run(["git", "apply", "--whitespace=nowarn", "-"], cwd=work,
                            input=patch, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
        if ap.returncode != 0:
            (out_dir / "worker.log").write_text(
                "base fail evidence kept; patch apply failed:\n" + ap.stderr, encoding="utf-8")
            return Result(job_id=job.job_id, backend=backend_name, outcome="failed",
                          base_sha=job.base_sha, reproduced_on_base=True,
                          logs_path=f"artifacts/{job.job_id}/worker.log",
                          unverified_items=["patch failed to apply on base"])
        # Stage only the fix and the regression files, never setup/build output.
        _seed_test_files(work, test_files | {path: versions[1] for path, versions in mixed_tests.items()})
        delivery_files = sorted(set(patch_files(patch)) | set(test_files) | set(mixed_tests))
        if delivery_files:
            _git(["add", "-A", "--", *delivery_files], work)
        delivery_patch = _git(["diff", "--cached", "--binary"], work).stdout
        fixed_rc, out, err, error = _replay_regression(
            cmd, plan, work, env, out_dir, "patched", rebuild=rebuild)
        if error is None and fixed_rc:
            error = "patched_test_failed", f"regression failed on the patch (exit {fixed_rc})"
        if error is None and plan and not all(marker.strip() in f"{out}\n{err}" for marker in
                                             [_test_case_identity(plan["base_failure"][0]),
                                              *plan["patched_success"]]):
            error = "patched_test_unconfirmed", "target test success not found in patched output"
        fixed = error is None
        failure = f"{error[0]} on patched: {error[1]}" if error else ""
        additional_checks = {}
        additional_check_output = {}
        if fixed and job.repo.lower() == "rclone/rclone":
            for command in (["go", "build"], ["make", "quicktest"]):
                name = " ".join(command)
                rc, check_out, check_err = _verify_command(
                    command, work, env, out_dir, "rclone-" + "-".join(command))
                additional_checks[name] = rc
                if rc != 0:
                    additional_check_output[name] = {"stdout_tail": check_out[-3000:],
                                                     "stderr_tail": check_err[-3000:]}
                    error = "additional_check_failed", f"required rclone check failed: {name} (exit {rc})"
                    failure = f"{error[0]}: {error[1]}"
                    break
        if fixed and not failure and delivery_files:
            after = _git(["diff", "HEAD", "--binary", "--", *delivery_files], work).stdout
            if after != delivery_patch:
                fixed = False
                error = "delivery_changed_during_verification", "tests/setup changed files intended for publication"
                failure = f"{error[0]}: {error[1]}"
        if root_cause:
            unverified.append(f"agent root cause: {root_cause[:200]}")
        (out_dir / "change.patch").write_bytes(delivery_patch.encode("utf-8"))
        (out_dir / "test-report.json").write_text(json.dumps({
            "schema_version": 1, "job_id": job.job_id, "base_sha": job.base_sha,
            "test_cmd": cmd, "base_returncode": base_rc, "patched_returncode": fixed_rc,
            "failure_kind": error[0] if error else None, "verification_plan": plan,
            # base 输出必须留存：否则"复现证据"无法审计，假阳性也无从发现
            "base_stdout_tail": base_out[-3000:], "base_stderr_tail": base_err[-3000:],
            "patched_stdout_tail": out[-3000:], "patched_stderr_tail": err[-3000:],
            "regression_test_files": sorted(baseline_tests),
            "delivery_files": patch_files(delivery_patch),
            "delivery_patch_sha256": hashlib.sha256((out_dir / "change.patch").read_bytes()).hexdigest(),
            "root_cause": root_cause,
            "pr_title": pr_title, "pr_body": pr_body,
            "repair_agent": job.repair_agent, "model": job.model,
            "additional_checks": additional_checks,
            "additional_check_output": additional_check_output,
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        (out_dir / "worker.log").write_text(
            f"=== base (rc={base_rc}) ===\n{base_out[-2000:]}\n{base_err[-2000:]}\n"
            f"=== patched (rc={fixed_rc}) ===\n{out[-4000:]}\n{err[-4000:]}",
            encoding="utf-8")

    return Result(job_id=job.job_id, backend=backend_name,
                  outcome="failed" if failure else "succeeded",
                  base_sha=job.base_sha,
                  reproduced_on_base=True,
                  fixed_on_patch=fixed,
                  regression_tests_passed=fixed,
                  patch_path=f"artifacts/{job.job_id}/change.patch",
                  test_report_path=f"artifacts/{job.job_id}/test-report.json",
                  logs_path=f"artifacts/{job.job_id}/worker.log",
                  unverified_items=[failure, *unverified] if failure else unverified)
