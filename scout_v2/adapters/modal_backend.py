"""Modal Sandbox 后端（P2）：临时 Sandbox、按需拉仓、无持久 Volume、结果走 stdout。

安全与费用约定（计划 §8/§12 + 用户约束 2026-10-25）：
- 创建前必须过 BudgetGuard（软阈值 + 计量读数 + 并发）；fail-closed。
- 只用临时 Sandbox，任务结束 terminate；不创建任何 Volume。
- Sandbox 内无 GitHub 写令牌、无模型密钥；repo 用公共 https 只读拉取固定 SHA。
- 结果经 stdout 单行信封回传（`##OSSCOUT_RESULT##` + JSON），避免依赖持久存储。

注意：Sandbox 运行时行为（镜像构建、stdin/stdout、poll 语义）以 scripts/modal_smoke.py
的实测为准；本文件对 API 细节做了防御式访问，未实测项记入 Result.unverified_items。
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
from pathlib import Path
from typing import Any

from ..budget import BudgetGuard

RESULT_MARKER = "##OSSCOUT_RESULT##"


@contextlib.contextmanager
def no_proxy_context():
    """Modal SDK 走 gRPC/WebSocket，经 HTTP 代理会 "Could not connect to the
    Modal server"（2026-11-01 试扫实录：p6_bug_hunter 三个仓库全部 submit 失败，
    与 10-25 成功轮的唯一环境差异是容器里多了 HTTPS_PROXY）。Modal 自有传输层，
    与宿主代理方案无关。内建在后端方法里，调用方无需（也不可能再忘）各自包裹。
    """
    keys = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
            "http_proxy", "https_proxy", "all_proxy")
    saved = {k: os.environ.pop(k) for k in keys if k in os.environ}
    try:
        yield
    finally:
        os.environ.update(saved)


class BackendDisabledError(RuntimeError):
    pass


class BudgetExceededError(RuntimeError):
    pass


class ModalSandboxBackend:
    """实现计划 §5 的 ComputeBackend 协议。"""

    def __init__(self, budget: BudgetGuard,
                 dockerfile: Path | None = None,
                 max_task_minutes: int = 30,
                 extra_env: dict[str, str] | None = None):
        self.budget = budget
        self.dockerfile = dockerfile or Path("docker/Dockerfile.worker")
        self.max_task_minutes = max_task_minutes
        # 额外环境变量（如 OPENCODE_API_KEY）由**控制器**在创建时显式传入——
        # 只进 Sandbox 进程环境，不落盘、不进 job payload、不进日志。
        self.extra_env = dict(extra_env or {})
        self._sandboxes: dict[str, Any] = {}
        self._reserved: dict[str, float] = {}

    # ---- 协议 ----

    def eligible(self, job: dict) -> bool:
        ok, _ = self.budget.can_start(job.get("max_estimated_compute_usd", 0.0))
        return ok

    def submit(self, job: dict) -> str:
        if not self.budget.enabled:
            raise BackendDisabledError("modal backend disabled by feature flag")
        cost = job.get("max_estimated_compute_usd", 0.0)
        ok, reason = self.budget.can_start(cost)
        if not ok:
            raise BudgetExceededError(reason)
        try:
            import modal  # noqa: F401
        except ImportError as e:
            raise BackendDisabledError(f"modal SDK not installed: {e}") from e
        return self._submit_real(job)

    def poll(self, remote_job_id: str) -> dict:
        sb = self._sandboxes[remote_job_id]
        with no_proxy_context():
            rc = sb.poll()
        if rc is None:
            return {"job_id": remote_job_id, "state": "running"}
        return {"job_id": remote_job_id, "state": "succeeded" if rc == 0 else "failed",
                "returncode": rc}

    def cancel(self, remote_job_id: str) -> None:
        sb = self._sandboxes.get(remote_job_id)
        if sb is not None:
            try:
                sb.terminate()
            except Exception:  # noqa: BLE001 — 已结束的 Sandbox terminate 会报错，忽略
                pass
            self._release(remote_job_id)

    def collect(self, remote_job_id: str) -> dict:
        sb = self._sandboxes[remote_job_id]
        with no_proxy_context():
            stdout = sb.stdout.read()
        envelope = parse_envelope(stdout)
        self._release(remote_job_id)
        if envelope is None:
            return {"job_id": remote_job_id, "outcome": "inconclusive",
                    "unverified_items": ["no result envelope in sandbox stdout"],
                    "stdout_tail": stdout[-2000:]}
        return envelope

    # ---- 内部 ----

    def _submit_real(self, job: dict) -> str:
        import modal

        with no_proxy_context():
            job_id = job["job_id"]
            # context_dir 必须是项目根：Dockerfile 里 COPY scout_v2/configs/docker
            image = modal.Image.from_dockerfile(
                str(self.dockerfile),
                context_dir=str(self.dockerfile.resolve().parents[1]))
            app = modal.App.lookup("oss-scout-worker", create_if_missing=True)
            job_b64 = base64.b64encode(
                json.dumps(job, ensure_ascii=False).encode("utf-8")).decode()
            # 镜像带 ENTRYPOINT：Sandbox 命令参数会追加到 entrypoint 之后（P6-A
            # 实测教训，rc=2 = entrypoint 拿 $1/$2 当 job/out 再 exec argparse 报错）。
            # 走 entrypoint 支持的 OSSCOUT_JOB_B64 环境变量通道，命令参数用无害占位。
            sb = modal.Sandbox.create(
                "/bin/true", app=app, image=image,
                env={"OSSCOUT_JOB_B64": job_b64, **self.extra_env},
                timeout=min(int(job.get("timeout_seconds", 1800)),
                            self.max_task_minutes * 60),
                cpu=job.get("cpu", 2),
                memory=job.get("memory_mb", 4096),
            )
        self._sandboxes[job_id] = sb
        self.budget.reserve(job.get("max_estimated_compute_usd", 0.0))
        self._reserved[job_id] = job.get("max_estimated_compute_usd", 0.0)
        return job_id

    def _release(self, job_id: str) -> None:
        amount = self._reserved.pop(job_id, None)
        if amount is not None:
            self.budget.release(amount)
            self._sandboxes.pop(job_id, None)


def parse_envelope(stdout: str) -> dict | None:
    """从 worker stdout 解析单行结果信封。找不到/解析失败 → None（调用方记 inconclusive）。

    兼容两种载体：Modal/本地 stdout（marker 在行首）、GitHub Actions 日志
    （每行有 `step \ttimestamp\t` 前缀）——因此按行内位置查找。
    """
    for line in stdout.splitlines():
        # 从后往前找：同一行可能多次出现 marker（如 Actions 日志里 echo 的源码行），
        # 最后一次出现之后才是真正的载荷
        idx = line.rfind(RESULT_MARKER)
        if idx < 0:
            continue
        payload = line[idx + len(RESULT_MARKER):].strip()
        try:
            env = json.loads(payload)
            if isinstance(env, dict) and "result" in env:
                return env
        except json.JSONDecodeError:
            continue  # 源码回显等伪 marker 行：跳过，继续找真正的输出行
    return None



# 兼容别名（P1 mock 测试与外部引用）
ModalBackend = ModalSandboxBackend
