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
import subprocess
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
RUN_TASK_MSG = ("Read the file OSSCOUT_TASK.md in the current directory and follow its "
                "instructions exactly. When done editing, stop.")


def _git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", check=check)


def _run_cmd(cmd: list[str], cwd: Path, env: dict) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=1800)
    except FileNotFoundError as exc:
        return 127, "", str(exc)
    except subprocess.TimeoutExpired as exc:
        return 124, str(exc.stdout or ""), str(exc.stderr or "")
    return p.returncode, p.stdout, p.stderr


def enforce_domain_b(work_dir: Path) -> str:
    """写入 opencode 权限配置：禁 shell 与网络，只允许文件编辑。返回声明字符串。"""
    cfg = {"permission": {"bash": "deny", "webfetch": "deny", "edit": "allow"}}
    (work_dir / "opencode.json").write_text(json.dumps(cfg), encoding="utf-8")
    return "opencode.json permission {bash:deny, webfetch:deny, edit:allow} written"


def resolve_model() -> str:
    """从 configs/model_policy.yaml 解析首选模型（fixed 模式取 primary_models[0]）。"""
    import yaml
    from .model_fallback import ModelPolicy
    root = Path(__file__).resolve().parents[2]
    cfg_path = root / "configs" / "model_policy.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    models = ModelPolicy.from_yaml(cfg).models()
    if not models:
        raise RuntimeError("no model configured in configs/model_policy.yaml")
    return models[0]


def opencode_patch_producer(job: Job, work_dir: Path, context: str,
                            client: OpenCodeClient | None = None,
                            model: str | None = None) -> tuple[str, list[str]]:
    """域 B：OpenCode 在 work_dir 内产出补丁。返回 (patch_text, notes)。"""
    cfg_path = work_dir / "opencode.json"
    original_cfg = cfg_path.read_bytes() if cfg_path.exists() else None
    notes = [enforce_domain_b(work_dir)]
    try:
        client = client or OpenCodeClient(timeout_seconds=1800, workdir=work_dir)
        model = model or resolve_model()
        (work_dir / TASK_FILE).write_text(FIX_PROMPT.format(context=context[:4000]),
                                          encoding="utf-8")
        ok, detail = client.run(model, RUN_TASK_MSG)
        if not ok:
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
    # 运行时产物即使被 add 也要从暂存区剔除（opencode.json 在 finally 已还原内容，
    # 但暂存区里可能还留着 add 时的旧快照）
    _git(["reset", "-q", "--", "opencode.json", TASK_FILE], work_dir, check=False)
    diff = _git(["diff", "--cached"], work_dir).stdout
    if not diff.strip():
        # notes 随异常带出，否则排查时丢线索（P5 演练教训）
        raise RuntimeError("opencode produced no changes (empty git diff); " + " | ".join(notes))
    return diff, notes


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
               patch_producer=None, env_base: dict | None = None) -> Result:
    """完整 repair 证据链。repo_dir 必须已 checkout 到 job.base_sha。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    if not test_cmd:
        return Result(job_id=job.job_id, backend="local", outcome="blocked",
                      base_sha=job.base_sha,
                      unverified_items=["no test_cmd — cannot verify fix"])
    unverified: list[str] = []
    env = sanitized_env(env_base, home=out_dir / "sandbox_home")

    # ---- 证据 1：base 上测试必须失败 ----
    base_rc, _, _ = _run_cmd(test_cmd, repo_dir, env)
    reproduced = base_rc not in (0, 127, 9009)
    if base_rc == 0:
        (out_dir / "worker.log").write_text("base test passed; nothing to fix\n", encoding="utf-8")
        return Result(job_id=job.job_id, backend="local", outcome="blocked",
                      base_sha=job.base_sha, reproduced_on_base=False,
                      logs_path=f"artifacts/{job.job_id}/worker.log",
                      unverified_items=["bug does not reproduce on base — refusing to fix blind"])
    if base_rc in (127, 9009):
        (out_dir / "worker.log").write_text(
            "base test command could not be run\n", encoding="utf-8")
        return Result(job_id=job.job_id, backend="local", outcome="failed",
                      base_sha=job.base_sha, reproduced_on_base=False,
                      logs_path=f"artifacts/{job.job_id}/worker.log",
                      unverified_items=[f"exit {base_rc}: command likely not runnable"])

    # ---- 域 B：在 base 的克隆里产补丁 ----
    patch = ""
    with tempfile.TemporaryDirectory() as td:
        work = Path(td) / "work"
        _git(["clone", "--quiet", str(repo_dir), str(work)], repo_dir)
        _git(["checkout", "--quiet", "--detach", job.base_sha], work, check=False)
        try:
            if patch_producer is None:
                patch, notes = opencode_patch_producer(job, work, context)
            else:
                patch, notes = patch_producer(job, work, context)
            unverified.extend(notes)
        except RuntimeError as e:
            (out_dir / "worker.log").write_text(str(e), encoding="utf-8")
            return Result(job_id=job.job_id, backend="local", outcome="failed",
                          base_sha=job.base_sha, reproduced_on_base=reproduced,
                          logs_path=f"artifacts/{job.job_id}/worker.log",
                          unverified_items=[str(e)[:300]])
        errs = check_patch_content(patch)
        if errs:
            (out_dir / "worker.log").write_text(patch[:2000], encoding="utf-8")
            return Result(job_id=job.job_id, backend="local", outcome="blocked",
                          base_sha=job.base_sha, reproduced_on_base=reproduced,
                          logs_path=f"artifacts/{job.job_id}/worker.log",
                          unverified_items=errs)

        # ---- 证据 2：干净 base + 补丁后测试必须通过 ----
        # producer 已在 work 里留下模型改动（可能已 staged），必须回到干净 base 再 apply，
        # 否则补丁重复应用必然失败（P5 演练暴露）。
        _git(["reset", "--hard", "--quiet", job.base_sha], work)
        _git(["clean", "-fdxq"], work)
        ap = subprocess.run(["git", "apply", "--whitespace=nowarn", "-"], cwd=work,
                            input=patch, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
        if ap.returncode != 0:
            (out_dir / "worker.log").write_text(
                "base fail evidence kept; patch apply failed:\n" + ap.stderr, encoding="utf-8")
            return Result(job_id=job.job_id, backend="local", outcome="failed",
                          base_sha=job.base_sha, reproduced_on_base=reproduced,
                          logs_path=f"artifacts/{job.job_id}/worker.log",
                          unverified_items=["patch failed to apply on base"])
        fixed_rc, out, err = _run_cmd(test_cmd, work, env)
        fixed = fixed_rc == 0
        (out_dir / "change.patch").write_text(patch, encoding="utf-8")
        (out_dir / "test-report.json").write_text(json.dumps({
            "schema_version": 1, "job_id": job.job_id, "base_sha": job.base_sha,
            "test_cmd": test_cmd, "base_returncode": base_rc, "patched_returncode": fixed_rc,
            "patched_stdout_tail": out[-3000:], "patched_stderr_tail": err[-3000:],
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        (out_dir / "worker.log").write_text(
            out[-4000:] + "\n" + err[-4000:], encoding="utf-8")

    if fixed and not reproduced:
        unverified.append("patched test passed but base evidence missing — do not claim fixed")
    return Result(job_id=job.job_id, backend="local",
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
