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

FIX_PROMPT = """You are fixing a bug in this repository.

Context (bug report / failing behavior):
{context}

Rules:
1. Explore the code read-only, then make the MINIMAL correct edit to fix the bug.
2. Do NOT run tests, do NOT install dependencies, do NOT run shell commands.
3. Do NOT modify test files. Do NOT reformat unrelated code.
4. When done editing, stop. Output one short line summarizing the root cause.
"""

# Windows 教训（P5 演练实测）：多行 prompt 经 argv 传给 opencode.CMD（批处理 shim）
# 会挂起或解析异常；Bash shell 下手动执行正常。长 prompt 写入工作目录文件，
# argv 只传单行指令。
TASK_FILE = "OSSCOUT_TASK.md"
RUN_TASK_MSG = ("Fix the bug described in OSSCOUT_TASK.md in this repository NOW: read it, "
                "then immediately make the minimal edit to the source file. Do not ask "
                "questions. When done editing, stop.")


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
    """写入 opencode 权限配置：禁 shell 与网络，只允许文件编辑。返回声明字符串。"""
    cfg = {"permission": {"bash": "deny", "webfetch": "deny", "edit": "allow"}}
    (work_dir / "opencode.json").write_text(json.dumps(cfg), encoding="utf-8")
    return "opencode.json permission {bash:deny, webfetch:deny, edit:allow} written"


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


def opencode_patch_producer(job: Job, work_dir: Path, context: str,
                            client: OpenCodeClient | None = None,
                            model: str | None = None,
                            ai_test_paths: list[str] | None = None) -> tuple[str, list[str]]:
    """域 B：OpenCode 在 work_dir 内产出补丁。返回 (patch_text, notes)。

    ai_test_paths：我们预先放进工作区的回归测试路径，必须排除在补丁之外
    （它们是验证手段，不是修复内容）。按实际路径排除而不是猜文件名。
    """
    ai_test_paths = list(ai_test_paths or [])
    cfg_path = work_dir / "opencode.json"
    original_cfg = cfg_path.read_bytes() if cfg_path.exists() else None
    notes = [enforce_domain_b(work_dir)]
    provider = _ensure_opencode_auth()
    notes.append(f"opencode auth: {provider}" if provider
                 else "opencode auth: no OPENCODE_API_KEY in env")
    try:
        # 600s：沙箱实测一次修复（文本模式）在 2-4 分钟内完成；
        # 留 600s 给大仓库，超时即判失败而不是把 Sandbox 占满 30 分钟。
        client = client or OpenCodeClient(timeout_seconds=600, workdir=work_dir)
        # job.model 由控制器下发：worker 镜像里没有 configs/（配置在控制器的 /data 卷），
        # 现场探测只在本地开发时兜底。
        model = model or (job.model or "").strip() or resolve_model()
        rules = repo_rules_block(job.repo)
        full_context = (rules + "\n\n" + context) if rules else context
        (work_dir / TASK_FILE).write_text(FIX_PROMPT.format(context=full_context[:4000]),
                                          encoding="utf-8")
        ok, detail = client.run(model, RUN_TASK_MSG)
        if not ok:
            # 模型 edit 后直接停（无 text 事件）不罕见——看 diff 是否非空再定论
            probe = _git(["diff", "--cached"], work_dir).stdout if False else None
            diff_early = _git(["diff"], work_dir).stdout
            if diff_early.strip():
                ok, detail = True, "(no text events; changes present)"
            else:
                raise RuntimeError(f"opencode repair run failed: {detail[:300]}")
        notes.append(f"model({model}) said: {detail[:200]}")
    finally:
        # 权限配置/任务文件是我们的运行时产物，绝不能混进补丁
        if original_cfg is None:
            cfg_path.unlink(missing_ok=True)
        else:
            cfg_path.write_bytes(original_cfg)
        (work_dir / TASK_FILE).unlink(missing_ok=True)

    # 用 --cached 才能把模型新建的文件纳入补丁；普通 `git diff` 会静默丢新文件
    _git(["add", "-A"], work_dir)
    # 运行时产物与 AI 生成的回归测试都不能进补丁：
    # - opencode.json 是我们的权限配置
    # - AI 回归测试是我们喂进去的验证用例，不属于修复改动
    # 测试文件名由 job.ai_test_files 的 key 决定（不一定是 issue 后缀那种），
    # 因此按实际路径排除；2026-10-28 实测 test_ai_regression.py 曾漏进补丁。
    excludes = ["opencode.json", TASK_FILE, *ai_test_paths]
    _git(["reset", "-q", "--", *excludes], work_dir, check=False)
    for p in ai_test_paths:
        _git(["-c", "core.quotepath=false", "reset", "-q", "--", p], work_dir, check=False)
    diff = _git(["diff", "--cached"], work_dir).stdout
    if not diff.strip():
        # notes 随异常带出，否则排查时丢线索（P5 演练教训）
        raise RuntimeError("opencode produced no changes (empty git diff); " + " | ".join(notes))
    return diff, notes


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
               test_cmd: list[str], context: str = "",
               patch_producer=None, env_base: dict | None = None,
               extra_test_files: dict[str, str] | None = None,
               backend_name: str = "local") -> Result:
    """完整 repair 证据链。repo_dir 必须已 checkout 到 job.base_sha。

    backend_name 只用于结果标签：worker 跑在 Modal / Actions 时不该谎称 local。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    if not test_cmd:
        return Result(job_id=job.job_id, backend=backend_name, outcome="blocked",
                      base_sha=job.base_sha,
                      unverified_items=["no test_cmd — cannot verify fix"])
    unverified: list[str] = []
    env = sanitized_env(env_base, home=out_dir / "sandbox_home")

    # ---- 证据 1：base 上测试必须失败 ----
    # AI 回归测试必须先落进 base 树，否则 base 阶段跑的是"文件不存在"，
    # pytest 以 usage error（rc=4）退出，而 rc!=0 会被当成"bug 复现"——
    # 2026-10-28 实测：base_returncode=4、patched=0，被记成 reproduced=true，
    # 于是任何"补丁后测试能过"的改动都会拿到一份伪造的复现证据。
    seeded = _seed_test_files(repo_dir, extra_test_files)
    try:
        base_rc, base_out, base_err = _run_cmd(test_cmd, repo_dir, env)
    finally:
        _unseed_test_files(repo_dir, seeded)
    # 退出码非 0 不等于"bug 复现了"：测试框架没装、模块导入失败、用例根本不存在
    # 都会给非 0。只有"测试确实跑了并且失败"才算复现。
    broken = _test_env_broken(base_out, base_err)
    reproduced = base_rc not in (0, 127, 9009) and broken is None
    if base_rc == 0:
        (out_dir / "worker.log").write_text("base test passed; nothing to fix\n", encoding="utf-8")
        return Result(job_id=job.job_id, backend=backend_name, outcome="blocked",
                      base_sha=job.base_sha, reproduced_on_base=False,
                      logs_path=f"artifacts/{job.job_id}/worker.log",
                      unverified_items=["bug does not reproduce on base — refusing to fix blind"])
    if base_rc in (127, 9009):
        (out_dir / "worker.log").write_text(
            "base test command could not be run\n", encoding="utf-8")
        return Result(job_id=job.job_id, backend=backend_name, outcome="failed",
                      base_sha=job.base_sha, reproduced_on_base=False,
                      logs_path=f"artifacts/{job.job_id}/worker.log",
                      unverified_items=[f"exit {base_rc}: command likely not runnable"])
    if broken is not None:
        # 环境问题：既不能声称复现，也不能声称修好。如实上报，不产出补丁。
        (out_dir / "worker.log").write_text(
            f"test environment unusable: {broken}\n\n"
            f"--- stdout ---\n{base_out[-2000:]}\n--- stderr ---\n{base_err[-2000:]}",
            encoding="utf-8")
        return Result(job_id=job.job_id, backend=backend_name, outcome="failed",
                      base_sha=job.base_sha, reproduced_on_base=False,
                      logs_path=f"artifacts/{job.job_id}/worker.log",
                      unverified_items=[f"test environment unusable: {broken}"])

    # ---- 域 B：在 base 的克隆里产补丁 ----
    patch = ""
    with tempfile.TemporaryDirectory() as td:
        work = Path(td) / "work"
        _git(["clone", "--quiet", str(repo_dir), str(work)], repo_dir)
        _git(["checkout", "--quiet", "--detach", job.base_sha], work, check=False)
        # AI 生成的回归测试直接落到 work 目录：两次验证运行都要能看到它，
        # 且它不属于修复补丁（下面 git diff 只取模型改动）
        for rel_path, content in (extra_test_files or {}).items():
            tf = work / rel_path
            tf.parent.mkdir(parents=True, exist_ok=True)
            tf.write_text(content, encoding="utf-8")
        test_paths = list((extra_test_files or {}).keys())
        try:
            if patch_producer is None:
                patch, notes = opencode_patch_producer(
                    job, work, context, ai_test_paths=test_paths)
            else:
                patch, notes = patch_producer(job, work, context)
            unverified.extend(notes)
        except RuntimeError as e:
            (out_dir / "worker.log").write_text(str(e), encoding="utf-8")
            return Result(job_id=job.job_id, backend=backend_name, outcome="failed",
                          base_sha=job.base_sha, reproduced_on_base=reproduced,
                          logs_path=f"artifacts/{job.job_id}/worker.log",
                          unverified_items=[str(e)[:300]])
        errs = check_patch_content(patch)
        if errs:
            (out_dir / "worker.log").write_text(patch[:2000], encoding="utf-8")
            return Result(job_id=job.job_id, backend=backend_name, outcome="blocked",
                          base_sha=job.base_sha, reproduced_on_base=reproduced,
                          logs_path=f"artifacts/{job.job_id}/worker.log",
                          unverified_items=errs)
        # 兜底：无论 producer 是谁，AI 测试文件都不该出现在**交付**的补丁里。
        # producer 内部已有排除（按 job.ai_test_files 的实际路径），这里再做一次
        # 后置过滤，防止自定义 producer 或未来改动把验证用例混进修复内容。
        #
        # 但**验证**必须用完整改动（含测试文件）：把测试剔掉再 apply，测试文件
        # 根本不存在，验证必然 "file or directory not found"（2026-10-28 实测）。
        fix_patch = (_strip_test_files_from_patch(patch, test_paths)
                     if test_paths else patch)

        # ---- 证据 2：干净 base + 补丁后测试必须通过 ----
        # producer 已在 work 里留下模型改动（可能已 staged），必须回到干净 base 再 apply，
        # 否则补丁重复应用必然失败（P5 演练暴露）。
        _git(["reset", "--hard", "--quiet", job.base_sha], work)
        _git(["clean", "-fdxq"], work)
        # 验证用完整补丁；测试文件若被 producer 排除掉了，就按 job 里的内容补回，
        # 否则 patched 阶段看不到它。
        for rel_path, content in (extra_test_files or {}).items():
            tf = work / rel_path
            tf.parent.mkdir(parents=True, exist_ok=True)
            tf.write_text(content, encoding="utf-8")
        ap = subprocess.run(["git", "apply", "--whitespace=nowarn", "-"], cwd=work,
                            input=fix_patch, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
        if ap.returncode != 0:
            (out_dir / "worker.log").write_text(
                "base fail evidence kept; patch apply failed:\n" + ap.stderr, encoding="utf-8")
            return Result(job_id=job.job_id, backend=backend_name, outcome="failed",
                          base_sha=job.base_sha, reproduced_on_base=reproduced,
                          logs_path=f"artifacts/{job.job_id}/worker.log",
                          unverified_items=["patch failed to apply on base"])
        fixed_rc, out, err = _run_cmd(test_cmd, work, env)
        fixed = fixed_rc == 0
        # 落盘的是交付补丁（纯修复），测试文件已经单独写回工作区
        (out_dir / "change.patch").write_text(fix_patch, encoding="utf-8")
        (out_dir / "test-report.json").write_text(json.dumps({
            "schema_version": 1, "job_id": job.job_id, "base_sha": job.base_sha,
            "test_cmd": test_cmd, "base_returncode": base_rc, "patched_returncode": fixed_rc,
            # base 输出必须留存：否则"复现证据"无法审计，假阳性也无从发现
            "base_stdout_tail": base_out[-3000:], "base_stderr_tail": base_err[-3000:],
            "patched_stdout_tail": out[-3000:], "patched_stderr_tail": err[-3000:],
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
