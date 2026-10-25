"""隔离环境最小复现（计划 §8.2.4-5）：原始版测试失败才允许称 reproduced。

信任域约束（计划 §10）：本模块运行**不可信项目代码**，环境变量必须脱敏——
任何 token/secret/密钥类变量不得进入子进程；结果以退出码 + 日志为证据。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ..schemas import Job, Result

# 子进程只保留白名单变量；含敏感关键字的全部剥除（双保险）
ALLOW_KEYS = {"PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME", "LANG", "LC_ALL",
              "PYTHONIOENCODING", "PYTHONHASHSEED", "WINDIR"}
DENY_MARKERS = ("TOKEN", "SECRET", "KEY", "PASSWORD", "CREDENTIAL",
                "GH_", "GITHUB_", "MODAL_", "OPENCODE", "ANTHROPIC", "OPENAI")


def sanitized_env(base: dict | None = None, home: str | Path | None = None) -> dict:
    """运行不可信代码的环境：完整父环境 - 敏感项（黑名单制）。

    历史教训（2026-10-25）：早期用白名单重建环境，剥掉的非敏感系统变量让
    Windows Winsock 初始化随机失败（WinError 10106，pytest 全灭）。改为
    继承完整环境、只剥敏感关键字项 + HOME/TMP 重定向——既保隔离又保可跑。
    """
    base = base if base is not None else dict(__import__("os").environ)
    env = {k: v for k, v in base.items()
           if not any(m in k.upper() for m in DENY_MARKERS)}
    if home is not None:
        env["HOME"] = str(home)
        env["TMP"] = env["TEMP"] = str(home)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def _run(cmd: list[str], cwd: Path, env: dict) -> tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=cwd, env=env, timeout=1800)
    return p.returncode, p.stdout, p.stderr


def run_reproduce(job: Job, repo_dir: Path, out_dir: Path, *,
                  test_cmd: list[str], runner=None, env_base: dict | None = None) -> Result:
    """在隔离环境中运行最小失败测试。

    证据链（计划 §16）：原始版（base）测试失败 → reproduced_on_base=True；
    补丁验证归 repair 模式，本函数不宣称 fixed。
    """
    if not test_cmd:
        return Result(job_id=job.job_id, backend="local", outcome="blocked",
                      base_sha=job.base_sha,
                      unverified_items=["no test_cmd in job — cannot reproduce"])
    runner = runner or _run
    out_dir.mkdir(parents=True, exist_ok=True)
    home = out_dir / "sandbox_home"
    home.mkdir(parents=True, exist_ok=True)
    env = sanitized_env(env_base, home=home)

    try:
        rc, stdout, stderr = runner(test_cmd, repo_dir, env)
    except FileNotFoundError:
        return Result(job_id=job.job_id, backend="local", outcome="failed",
                      base_sha=job.base_sha,
                      unverified_items=[f"command not found: {test_cmd[0]}"])
    except subprocess.TimeoutExpired:
        return Result(job_id=job.job_id, backend="local", outcome="timed_out",
                      base_sha=job.base_sha,
                      unverified_items=["reproduce test exceeded 1800s"])

    report = {
        "schema_version": 1,
        "job_id": job.job_id,
        "base_sha": job.base_sha,
        "test_cmd": test_cmd,
        "returncode": rc,
        "env_keys": sorted(env.keys()),   # 只记录键名，记录值会泄密
        "stdout_tail": stdout[-4000:],
        "stderr_tail": stderr[-4000:],
    }
    (out_dir / "test-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "worker.log").write_text(stdout[-4000:] + "\n" + stderr[-4000:], encoding="utf-8")

    # 退出码非 0 = 测试失败 = bug 在 base 上复现；但命令本身跑不起来（127/文件缺失）
    # 不算复现证据，交给控制器看日志判别
    reproduced = rc != 0 and rc != 127
    unverified = [] if rc not in (127, 9009) else [f"exit {rc}: command likely not runnable"]
    return Result(job_id=job.job_id, backend="local", outcome="succeeded",
                  base_sha=job.base_sha,
                  reproduced_on_base=reproduced,
                  test_report_path=f"artifacts/{job.job_id}/test-report.json",
                  logs_path=f"artifacts/{job.job_id}/worker.log",
                  unverified_items=unverified)
