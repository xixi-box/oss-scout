"""Agent-owned repair, testing and self-check; mechanical delivery checks only."""

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
   test dirs / CI config). Do not assume a framework. Read the full issue
   discussion and repository contribution rules before choosing a direction.
   Check sibling call paths and run the relevant existing tests before changing
   behavior. Budget time for reproduction, repair, related checks and reporting;
   do not spend the whole task on a broad build or unrelated full-repository suite.
   Trace the REAL entrypoint through parsing, copying/serialization, adapters and
   sibling consumers to the faulty behavior. For example, metadata attached to
   an ideal intermediate object is not coverage of a real shallow copy that
   drops that metadata. Do not assume a field exists on actual runtime inputs.

2. WRITE A REGRESSION TEST in the repo's OWN style and location, following its
   existing conventions. It must fail on the current code for the reason described
   in the bug report. Run it. If it does not fail, your test does not capture the
   bug — fix the test, not the source.

3. FIX the bug with the smallest correct change. Re-run your test until it passes.

4. CHECK that you did not break things: run the affected existing test module or
   package sub-suite, including sibling call paths. Read the full package test
   command, but run the full package only when its cost fits the remaining time
   or its contribution rules require it. Record the exact scope and any omitted
   broader checks. Existing tests often encode the
   current behaviour as a contract, and a change that looks correct can contradict
   one. If an existing test asserts the behaviour you are changing, STOP and
   reconsider: either the fix belongs at a different layer, or that test is part of
   what this change is meant to update. Report which one in "root_cause".
   Check the concrete before/after values for adjacent and already-valid inputs.
   For joining/normalization, preserve meaningful separators and existing text;
   for mutation/copying, test the actual producer's aliasing and repeated calls,
   rather than supplying cleaner independent objects only in the regression.

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

Record the commands and results you actually ran; retain machine reports when
available, but no particular runner, report format or literal output marker is
required. The program will NOT rerun your tests or ask another AI to review.
You are responsible for reproducing the bug, verifying the fix, and checking
related paths and compatibility. Missing dependencies or build/command errors
are not evidence of the reported bug. Never fabricate logs or successful checks.
Reports are diagnostic artifacts and must not enter the PR patch.
Update OSSCOUT_RESULT.json after reproduction and again after patched testing;
save findings as you go so interruption does not lose completed evidence.
Save actual command output to OSSCOUT_BASE.log and OSSCOUT_PATCHED.log as you run
it (preserving the command's real exit status). These diagnostic logs are not
part of the patch. Leave enough time to complete and check the handoff below.
The base failure must demonstrate the reported wrong behavior, not a missing
new helper module, import error, setup error, or compilation failure. When
extracting a helper, first reproduce the bug through the existing base behavior;
do not make the regression depend on a module that only exists after your fix.
Write real JSON. Include the commands, results and limitations truthfully.
Include a "coverage" object with:
{{"entrypoint": {{"path":"<real source path>","symbol":"<entry function>"}},
  "call_chain": [{{"path":"<source path>","symbol":"<function along the actual path>"}}],
  "regression_case":"<test_report.case, or base_failure's first entry>",
  "scenarios":["<actual failing path and related boundary/sibling scenarios exercised>"],
  "limitations":["<paths/checks not exercised; empty only when none>"]}}.
Include the entrypoint in call_chain. Inspect the actual source and verify that
your test follows this path. Inline Rust tests and standalone test programs stay
in the complete delivery patch; there is no external source/test splitting.
For a bug in an existing test itself, run that original test unchanged on base,
then the repaired version under the SAME case identity on patched. Explain why
the test is wrong; preserve its behavioral assertions and coverage. Do not make
a failure pass by skipping it, deleting assertions, or arbitrarily raising a
timeout. Report measured timing/environment evidence for timing-related fixes.
The regression tests will be delivered in the PR with the fix. Keep them in the
project's normal test locations and run the affected existing tests too.
Before finishing, SELF-CHECK the final diff, real entrypoints and sibling paths,
edge cases, compatibility, test coverage, CONTRIBUTING rules and the repository's
PR template (.github/PULL_REQUEST_TEMPLATE.md, .github/pull_request_template.md,
or the applicable template in .github/PULL_REQUEST_TEMPLATE/).
Add pr_title and pr_body to OSSCOUT_RESULT.json. Write a concise English change
title, not a copy of the bug report's title. In pr_body explain the observed
problem and root cause, the concrete fix and relevant compatibility implications.
Follow the applicable PR template, preserving its headings and meaningful fields.
Use exactly one {{VERIFIED_TESTS}} placeholder for all test commands and test
results: the publisher replaces it with your reported commands and outcomes,
clearly attributed to your self-test report. Do not
invent checks, mark unverified checklist items as complete, or claim CI is green.
Do not add generic AI-assistance or oss-scout promotional text. Include any
required truthful disclosure. Keep the description focused on this diff.
If the only remaining blocker for a public, non-security issue is a publication
rule (such as contributor-written motivation or PR text), finish the permitted
repair and tests and write pr_title and pr_body as an AI-generated working draft
for the contributor, including suggested motivation. Keep the patch and logs.
Use status="blocked_publication", publication_ready=false and a concrete blocker.
Do not complete an attestation the contributor must make or claim human authorship.
If a real prerequisite or unavailable reproducer prevents a truthful repair,
set status="blocked", publication_ready=false and blocker="<observed reason>"
in OSSCOUT_RESULT.json. Do not manufacture a test or success report. Otherwise
preserve and complete the top-level JSON fields above; do not replace them with
a prose summary or nest them inside a separate result/verification object.
You do not need to create a commit, configure Git identity, push or open a PR.
The trusted publisher handles Git author, committer and DCO sign-off.
Set publication_ready=true only after your tests and self-check are complete,
and add self_check and test_results as lists of strings describing actual
checks and outcomes, e.g. test_results=["cargo test regression: passed"].
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
    "finished, complete OSSCOUT_RESULT.json with the top-level handoff schema "
    "in the task file, including observed test evidence, coverage and PR fields. "
    "Report genuine blockers using the structured blocked handoff.")


def _git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", check=check)


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
        if repo.lower() == "qdrant/qdrant":
            lines.append(
                "Qdrant-only publication instruction: the operator authorizes the Agent to draft "
                "the PR motivation and body. If contributor-written motivation is the only "
                "remaining publication blocker, deliver the normal publishable handoff after "
                "completing tests and self-check, rather than using blocked_publication. "
                "Explain what problem the patch solves and why it matters. The publisher "
                "will disclose AI authorship. Do not claim human authorship or human review. "
                "All other publication prerequisites still apply.")
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
            if isinstance(value, str) and value.strip() and len(value.strip()) <= limit:
                out[key] = value.strip()
        cmd = data.get("test_cmd") or data.get("command")
        if isinstance(cmd, list):
            import shlex
            cmd = shlex.join(str(x) for x in cmd)
        if isinstance(cmd, str) and cmd.strip():
            out["test_cmd"] = cmd.strip()
        tf = data.get("test_file") or data.get("test_path")
        if isinstance(tf, str) and tf.strip():
            out["test_file"] = tf.strip()
        rc = data.get("root_cause") or data.get("cause")
        if isinstance(rc, str) and rc.strip():
            out["root_cause"] = rc.strip()[:300]
        for key in ("setup_steps", "base_failure", "patched_success", "test_report", "coverage", "test_results", "self_check"):
            if key in data:
                out[key] = data[key]
        for key in ("status", "publication_ready", "publication_allowed", "blocker", "blocked_reason", "blockers"):
            if key in data:
                out[key] = data[key]
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
        patch       完整交付补丁（含回归测试与二进制文件）
        test_cmd    agent 实际用过的测试命令（argv）
        agent_report Agent 自测与自查记录
        root_cause  一句话归因
        notes       过程声明

    Agent 自主完成测试和自查，程序只收集完整补丁与真实日志。
    """
    managed_client = client is None
    delivery_deadline = time.monotonic() + 2400
    completion = {}
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
        # 40 分钟总预算：35 分钟实现，最多预留 5 分钟补齐真实交付材料。
        if client is None:
            if job.repair_agent == "codex":
                from .codex_client import CodexClient
                client = CodexClient(timeout_seconds=2100, workdir=work_dir,
                                     diagnostics_dir=diagnostics_dir)
            else:
                client = OpenCodeClient(timeout_seconds=2100, workdir=work_dir,
                                        diagnostics_dir=diagnostics_dir)
        model = model or (job.model or "").strip()
        if not model and job.repair_agent == "opencode":
            model = resolve_model()
        rules = repo_rules_block(job.repo)
        (work_dir / TASK_FILE).write_text(
            FIX_PROMPT.format(context=context, rules=rules or "(none)"),
            encoding="utf-8")
        ok, detail = client.run(model, RUN_TASK_MSG)
        detail = _mask_opencode_key(detail or "")
        from .codex_client import CODEX_COMMUNICATION_RETRY_PREFIX
        from .opencode_client import MODEL_COMMUNICATION_RETRY_PREFIX
        communication_prefixes = (CODEX_COMMUNICATION_RETRY_PREFIX, MODEL_COMMUNICATION_RETRY_PREFIX)
        if not ok and detail.startswith(communication_prefixes):
            raise RuntimeError(detail[:1000])
        diff_early = _git(["diff"], work_dir).stdout
        # 2026-10-29 实测：模型有时只回一句"I'll start by reading the task file"
        # 就以 rc=0 退出，什么都没做（langchain#41448 那次只跑了 36 秒）。
        # 这不是失败也不是成功——重试一次，用更直接的指令逼它动手。
        if not diff_early.strip() and "I'll start" in (detail or "").lower()[:200]:
            notes.append("agent replied with a progress line and exited; retrying once")
            nudge = (RUN_TASK_MSG + " You already said you would start - actually do it "
                     "now. No narration.")
            if managed_client:
                remaining = delivery_deadline - 300 - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("agent repair budget exhausted before handoff")
                client.timeout = max(1, min(2100, int(remaining)))
            ok, detail = client.run(model, nudge)
            detail = _mask_opencode_key(detail or "")
            if not ok and detail.startswith(communication_prefixes):
                raise RuntimeError(detail[:1000])
            diff_early = _git(["diff"], work_dir).stdout
        if not ok and not diff_early.strip():
            raise RuntimeError(f"{job.repair_agent} repair run failed: {detail[:300]}")
        if ok and managed_client:
            from .delivery_materials import COMPLETION_TASK, material_issues, reported_blocker
            markers = _read_result_file(work_dir)
            blocker = reported_blocker(markers)
            if blocker and markers.get("status") != "blocked_publication":
                raise RuntimeError("agent_reported_blocker: " + blocker)
            issues = material_issues(markers, work_dir)
            completion = {"initial_issues": issues, "attempts": 0}
            if issues and time.monotonic() < delivery_deadline:
                check_file = work_dir / "OSSCOUT_DELIVERY_CHECK.json"
                check_file.write_text(json.dumps({"missing_or_invalid": issues,
                    "cli_events": str(diagnostics_dir / "codex-events.jsonl")
                        if job.repair_agent == "codex" and diagnostics_dir else None}, ensure_ascii=False), encoding="utf-8")
                from .delivery_materials import redact_log
                (work_dir / "OSSCOUT_AGENT_INITIAL.log").write_text(redact_log(detail), encoding="utf-8")
                # Keep the same task budget and reserve at most five minutes for
                # one material-completion pass. No credential/protocol changes.
                client.timeout = max(1, min(300, int(delivery_deadline - time.monotonic())))
                completion["attempts"] = 1
                completion_ok, completion_detail = client.run(model, COMPLETION_TASK)
                detail = _mask_opencode_key(completion_detail or "")
                if not completion_ok and detail.startswith(communication_prefixes):
                    raise RuntimeError(detail[:1000])
                ok = completion_ok
                completion["completion_ok"] = completion_ok
                markers = _read_result_file(work_dir)
                blocker = reported_blocker(markers)
                if blocker and markers.get("status") != "blocked_publication":
                    raise RuntimeError("agent_reported_blocker: " + blocker)
            completion["remaining_issues"] = material_issues(markers, work_dir)
            if diagnostics_dir:
                diagnostics_dir.mkdir(parents=True, exist_ok=True)
                (diagnostics_dir / "delivery-preflight.json").write_text(
                    json.dumps(completion, ensure_ascii=False, indent=1), encoding="utf-8")
            if completion["remaining_issues"]:
                raise RuntimeError("delivery_materials_incomplete: " + "; ".join(completion["remaining_issues"])[:900])
        notes.append(f"model({model}) said: {detail[:200]}")
    finally:
        if diagnostics_dir:
            from .delivery_materials import _source_file, redact_log
            for name in ("OSSCOUT_BASE.log", "OSSCOUT_PATCHED.log", "OSSCOUT_AGENT_INITIAL.log", RESULT_FILE):
                try:
                    source = work_dir / name
                    if _source_file(work_dir, name) and source.stat().st_size <= 5_000_000:
                        diagnostics_dir.mkdir(parents=True, exist_ok=True)
                        destination = "agent-handoff.json" if name == RESULT_FILE else name
                        (diagnostics_dir / destination).write_text(redact_log(
                            source.read_text(encoding="utf-8", errors="replace")), encoding="utf-8")
                except OSError:
                    pass  # Diagnostic persistence must not replace the real result.
        if original_cfg is None:
            cfg_path.unlink(missing_ok=True)
        else:
            cfg_path.write_bytes(original_cfg)
        (work_dir / TASK_FILE).unlink(missing_ok=True)
        (work_dir / "OSSCOUT_DELIVERY_CHECK.json").unlink(missing_ok=True)

    # 优先读 agent 落盘的结果文件，读不到再退回解析 stdout
    markers = _read_result_file(work_dir) or _parse_markers(detail)

    _git(["add", "-A"], work_dir)
    changed = _git(["diff", "--cached", "--name-only"], work_dir).stdout.splitlines()
    from .publish_remote import _JUNK_PATH, filter_patch_junk
    excluded = {"opencode.json", TASK_FILE, RESULT_FILE, ".oss-scout-result.json"}
    report = markers.get("test_report")
    if isinstance(report, dict) and isinstance(report.get("path"), str):
        excluded.add(report["path"])
    excludes = [path for path in changed if path in excluded or _JUNK_PATH.search(path)]
    if excludes:
        _git(["reset", "-q", "--", *excludes], work_dir)
    patch = filter_patch_junk(_git(["diff", "--cached", "--binary"], work_dir).stdout)
    if not patch.strip():
        raise RuntimeError(f"agent produced no deliverable changes; agent said: {detail[:400]}")
    return {
        "patch": patch, "test_cmd": _split_cmd(markers.get("test_cmd", "")),
        "agent_report": markers, "verification_mode": "agent",
        "root_cause": markers.get("root_cause", ""),
        "pr_title": markers.get("pr_title", ""), "pr_body": markers.get("pr_body", ""),
        "coverage": markers.get("coverage") or {}, "delivery_preflight": completion,
        "notes": notes, "baseline": baseline, "agent_output": detail or "",
        "agent_failure": (detail or f"{job.repair_agent} did not complete successfully") if not ok else "",
        "changed_files": changed, "markers_found": bool(markers),
    }


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


def _checkout_repair_tree(repo_dir: Path, work: Path, base_sha: str) -> None:
    _git(["clone", "--quiet", str(repo_dir), str(work)], repo_dir)
    _git(["checkout", "--quiet", "--detach", base_sha], work)


# 这些命令的退出码只反映"某个字符串/文件在不在"，跟被测代码的行为无关。
# 允许它们出现在复合命令里（如 `cmd && pytest`），但整条命令不能只有它们。
_NON_VERIFYING_BINARIES = {
    "grep", "egrep", "fgrep", "rg", "ag", "test", "[", "true", "false",
    "ls", "dir", "cat", "head", "tail", "wc", "stat", "find", "git", "echo",
    "printf", "sleep", "curl", "wget", "diff", "cmp",
    # 目录切换本身不验证任何行为：`cd sub && grep -q x f` 的退出码仍只反映 grep
    "cd", "export", "source", ".", "set", "pushd", "popd", "mkdir",
}


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
    """Collect the Agent's complete delivery without rerunning tests or AI review.

    The Agent owns tests and self-check. This worker only checks the delivery's
    safety and records provenance. Legacy evidence booleans stay false: no
    independent regression replay has taken place.
    """
    from .quality_gate import check_delivery_scope, patch_files
    from .delivery_materials import reported_blocker, redact_log

    out_dir.mkdir(parents=True, exist_ok=True)
    def stopped(reason, outcome="failed"):
        (out_dir / "worker.log").write_text(redact_log(reason), encoding="utf-8")
        return Result(job_id=job.job_id, backend=backend_name, outcome=outcome,
                      base_sha=job.base_sha, verification_mode="agent",
                      logs_path=f"artifacts/{job.job_id}/worker.log",
                      unverified_items=[reason[:1000]])

    with ExitStack() as workspace:
        work = Path(workspace.enter_context(_RepairWorkspace(out_dir))) / "work"
        _checkout_repair_tree(repo_dir, work, job.base_sha)
        # Old injected test payloads are not part of the production screening path.
        for rel, content in (extra_test_files or {}).items():
            from .quality_gate import check_patch_path
            if check_patch_path(rel):
                return stopped("unsafe supplied test path", "blocked")
            path = work / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        try:
            produced = (opencode_patch_producer(job, work, context, diagnostics_dir=out_dir)
                        if patch_producer is None else patch_producer(job, work, context))
        except RuntimeError as exc:
            return stopped(str(exc))
        if not isinstance(produced, dict):
            patch, notes = produced
            produced = {"patch": patch, "notes": notes}
        if produced.get("agent_output"):
            (out_dir / "agent-output.txt").write_text(redact_log(produced["agent_output"]), encoding="utf-8")
        if produced.get("agent_failure"):
            return stopped(f"{job.repair_agent} repair run failed: {produced['agent_failure']}")
        agent_report = produced.get("agent_report") or {}
        blocker = reported_blocker(agent_report)
        patch = produced.get("patch") or ""
        if blocker and agent_report.get("status") == "blocked_publication":
            # Manual handoff is diagnostic only. No patch_path or test_report_path
            # reaches the normal publisher or the automatic recovery path.
            if (patch.strip() and not check_patch_content(patch) and not check_delivery_scope(patch)
                    and redact_log(patch) == patch):
                (out_dir / "manual-review.patch").write_bytes(patch.encode("utf-8"))
            return stopped("manual_publication_required: " + blocker, "blocked")
        if blocker or agent_report.get("publication_ready") is False:
            return stopped("agent_reported_blocker: " + (blocker or "Agent did not approve publication"), "blocked")
        if not patch.strip():
            return stopped("agent produced no deliverable changes", "blocked")
        errors = check_patch_content(patch) + check_delivery_scope(patch)
        if errors:
            return stopped("; ".join(errors), "blocked")
        # Complete patch is delivered unchanged, including tests/binary fixtures.
        # No source/test classification or additional build/test command runs here.
        (out_dir / "change.patch").write_bytes(patch.encode("utf-8"))
        cmd = produced.get("test_cmd") or list(test_cmd or [])
        report = {
            "schema_version": 2, "verification_mode": "agent",
            "job_id": job.job_id, "base_sha": job.base_sha,
            "test_cmd": cmd, "agent_report": agent_report,
            "root_cause": produced.get("root_cause") or agent_report.get("root_cause", ""),
            "pr_title": produced.get("pr_title") or agent_report.get("pr_title", ""),
            "pr_body": produced.get("pr_body") or agent_report.get("pr_body", ""),
            "coverage": produced.get("coverage") or agent_report.get("coverage", {}),
            "regression_test_files": [agent_report["test_file"]] if agent_report.get("test_file") else [],
            "delivery_preflight": produced.get("delivery_preflight") or {},
            "delivery_files": patch_files(patch),
            "delivery_patch_sha256": hashlib.sha256(patch.encode("utf-8")).hexdigest(),
            "repair_agent": job.repair_agent, "model": job.model,
        }
        (out_dir / "test-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        (out_dir / "worker.log").write_text(
            "Agent completed repair, tests and self-check. No independent test replay or extra AI review.\n"
            "See agent-handoff.json, Agent command logs and CLI diagnostics for reported results.\n",
            encoding="utf-8")
    return Result(job_id=job.job_id, backend=backend_name, outcome="succeeded",
                  base_sha=job.base_sha, verification_mode="agent",
                  patch_path=f"artifacts/{job.job_id}/change.patch",
                  test_report_path=f"artifacts/{job.job_id}/test-report.json",
                  logs_path=f"artifacts/{job.job_id}/worker.log",
                  unverified_items=["Tests and self-check performed by the Agent; not independently replayed."])
