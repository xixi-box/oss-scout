"""GitHub Actions 后端（P3）：项目关联工作的 workflow_dispatch 执行通道。

范围约束（计划 §1/§9）：
- 只用于**项目关联**的 repair/review/ci 与本系统自身测试仓库的验证；
  execution_scope=broad_scan 一律 ineligible（Actions 不是免费扫描云）。
- 提交 = 在指定仓库 dispatch 指定 workflow（inputs 携带 job JSON 的 base64）；
  收集 = 下载 run 的 artifact 并做 job_id/SHA 一致性校验（复用 quality_gate）。
- 所有 gh 调用可注入 runner 便于测试；真实调用尊重 TLS 抖动下的重试。
"""

from __future__ import annotations

import base64
import io
import json
import subprocess
import time
import zipfile
from pathlib import Path
from typing import Callable

from ..schemas import Job, Result

GH = Callable[[list[str]], tuple[int, str]]     # gh api runner：runner(args) -> (rc, stdout)
DL = Callable[[list[str]], tuple[int, str]]     # gh run runner：runner(args) -> (rc, output)

# 执行 job 的仓库（我们自己的）。目标仓库写在 job payload 里。
DEFAULT_RUNNER_REPO = "xixi-box/oss-scout"
# artifact / result.json 的固定名字（与 workers/core.py 一致）
RESULT_FILE = "result.json"


def _default_gh(args: list[str], retries: int = 4) -> tuple[int, str]:
    last = ""
    for attempt in range(retries + 1):
        p = subprocess.run(["gh", "api"] + args, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if p.returncode == 0:
            return 0, p.stdout
        last = p.stderr
        time.sleep(min(30, (attempt + 1) * 5))
    return 1, last


def _default_download(args: list[str], retries: int = 3) -> tuple[int, str]:
    for attempt in range(retries + 1):
        p = subprocess.run(["gh"] + args, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if p.returncode == 0:
            return 0, ""
        last = p.stderr
        time.sleep(min(20, (attempt + 1) * 5))
    return 1, last


class ActionsBackend:
    def __init__(self, *, enabled: bool = False, repo: str = "",
                 workflow: str = "worker-smoke.yml", ref: str = "main",
                 workflow_exists: bool = False, gh_runner: GH | None = None,
                 download_runner: DL | None = None,
                 artifact_dir: Path | None = None,
                 runner_repo: str = DEFAULT_RUNNER_REPO):
        self.enabled = enabled
        # runner_repo = 执行 job 的仓库（我们自己的）；repo 仅作历史兼容参数保留。
        # 2026-10-28 修正：dispatch 必须发到自己仓库，不能要求第三方仓库里有我们的
        # workflow（既不可行也违背最小侵入）。
        self.runner_repo = runner_repo or repo or DEFAULT_RUNNER_REPO
        self.repo = self.runner_repo
        self.workflow = workflow
        self.ref = ref
        self.workflow_exists = workflow_exists
        self.gh = gh_runner or _default_gh
        self.download = download_runner or _default_download
        self.artifact_dir = artifact_dir or Path("artifacts")

    # ---- eligibility（能力全对称裁定 2026-10-25：五模式皆可承载；唯一硬线是
    #      broad_scan 恒拒——ToS 禁止公共 runner 批量计算无关第三方项目）----

    def eligible(self, job: dict) -> bool:
        if not self.enabled or not self.runner_repo or not self.workflow_exists:
            return False
        if job.get("execution_scope") == "broad_scan":
            return False
        if job.get("mode") not in {"repair", "review", "ci", "scan", "reproduce"}:
            return False
        # 目标仓库与固定 SHA 由 job payload 携带，缺了 runner 无从 checkout
        return bool(job.get("repo") and job.get("base_sha"))

    # ---- 协议 ----

    def submit(self, job: dict) -> str:
        if not self.enabled:
            raise RuntimeError("actions backend disabled by feature flag")
        if not self.eligible(job):
            raise RuntimeError("actions backend ineligible for this job (scope/mode/workflow)")
        payload = base64.b64encode(json.dumps(job, ensure_ascii=False).encode("utf-8")).decode()
        rc, out = self.gh(["-X", "POST",
                           f"repos/{self.repo}/actions/workflows/{self.workflow}/dispatches",
                           "-f", f"ref={self.ref}",
                           "-f", f"inputs[oss_job_b64]={payload}",
                           "-f", f"inputs[job_id]={job['job_id']}"])
        if rc != 0:
            raise RuntimeError(f"workflow_dispatch failed: {out[:200]}")
        run_id = self._find_run(job["job_id"])
        if run_id is None:
            raise RuntimeError("dispatch accepted but run not found in time")
        return str(run_id)

    def poll(self, remote_job_id: str) -> dict:
        rc, out = self.gh([f"repos/{self.repo}/actions/runs/{remote_job_id}"])
        if rc != 0:
            return {"job_id": remote_job_id, "state": "unknown", "error": out[:200]}
        run = json.loads(out)
        if run.get("status") != "completed":
            return {"job_id": remote_job_id, "state": "running"}
        conclusion = run.get("conclusion") or "unknown"
        return {"job_id": remote_job_id,
                "state": "succeeded" if conclusion == "success" else "failed",
                "conclusion": conclusion}

    def cancel(self, remote_job_id: str) -> None:
        self.gh(["-X", "DELETE", f"repos/{self.repo}/actions/runs/{remote_job_id}"])

    def collect(self, remote_job_id: str) -> dict:
        """收集结果。artifact 优先，任何失败都回退到 stdout 信封。

        artifact 存储有账号级配额，撞上就整包失败（2026-10-28 实录），
        因此这里不能把 artifact 失败当作终局——日志里的信封同样带 patch。
        """
        rc, out = self.gh([f"repos/{self.runner_repo}/actions/runs/{remote_job_id}/artifacts"])
        if rc == 0:
            try:
                artifacts = json.loads(out).get("artifacts", [])
            except json.JSONDecodeError:
                artifacts = []
            if artifacts:
                dest = self.artifact_dir / f"run-{remote_job_id}"
                rc2, _err = self.download(["run", "download", remote_job_id,
                                           "--repo", self.runner_repo, "--dir", str(dest)])
                if rc2 == 0:
                    _found, data = self._load_result(dest)
                    if data is not None:
                        return data
        # artifact 不可用（不存在 / 配额满 / 下载失败 / 无 result.json）→ 读日志信封
        return self._collect_from_log(remote_job_id)

    @staticmethod
    def _load_result(dest: Path) -> tuple[Path | None, dict | None]:
        """artifact 里找 result.json，并把同目录的补丁与测试报告一并带出。

        runner 是一次性文件系统：patch 只能靠 artifact 回传，控制器拿到才能开 PR。
        """
        results = sorted(dest.rglob(RESULT_FILE))
        if not results:
            return None, None
        p = results[0]
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return p, None
        if not isinstance(data, dict):
            return p, None
        for name, key in (("change.patch", "patch"),
                          ("test-report.json", "test_report")):
            f = p.parent / name
            if f.exists():
                data[key] = f.read_text(encoding="utf-8", errors="replace")
        return p, data

    def _collect_from_log(self, remote_job_id: str) -> dict:
        """无 artifact 时的主通道：从 run 日志里解析 stdout 信封。

        artifact 存储有账号级配额，撞上就整包丢失（2026-10-28 实录）。信封里
        已带 patch 与 test_report，必须原样返回——早期实现只摊平了 result 和
        test_report，把 patch 丢了，于是配额一满就拿不到补丁，无法开 PR。
        """
        from ..adapters.modal_backend import parse_envelope
        text = self._logs_text(remote_job_id)
        if text is None:
            return {"outcome": "inconclusive",
                    "unverified_items": ["no artifacts and logs unavailable"]}
        env = parse_envelope(text)
        if env is None:
            return {"outcome": "inconclusive",
                    "unverified_items": ["no result envelope in run logs"]}
        out: dict = dict(env.get("result") or {})
        for key in ("patch", "test_report", "findings"):
            if env.get(key) is not None:
                out[key] = env[key]
        return out

    def _logs_text(self, remote_job_id: str) -> str | None:
        """拉取 run 日志全文。gh api logs 端点返回 zip；`gh run view --log` 在管道下
        会 rc=0 但输出恒空（实测 2026-10-25），因此直接抓 zip 字节流本地解包。"""
        p = subprocess.run(["gh", "api", f"repos/{self.repo}/actions/runs/{remote_job_id}/logs"],
                           capture_output=True)
        if p.returncode != 0 or p.stdout[:2] != b"PK":
            return None
        try:
            zf = zipfile.ZipFile(io.BytesIO(p.stdout))
            return "\n".join(zf.read(n).decode("utf-8", "replace")
                             for n in zf.namelist() if n.endswith(".txt"))
        except (zipfile.BadZipFile, OSError):
            return None

    # ---- 内部 ----

    def _find_run(self, job_id: str, attempts: int = 8) -> int | None:
        for _ in range(attempts):
            rc, out = self.gh([f"repos/{self.repo}/actions/workflows/{self.workflow}/runs",
                               "-X", "GET", "-f", "per_page=10"])
            if rc == 0:
                for run in json.loads(out).get("workflow_runs", []):
                    if run.get("event") == "workflow_dispatch":
                        return run["id"]
            time.sleep(5)
        return None


def verify_collected(collected: dict, job: Job) -> list[str]:
    """控制器导入前的最小校验：job_id/base_sha 必须一致（计划 §16）。"""
    errs = []
    if collected.get("job_id") != job.job_id:
        errs.append(f"job_id mismatch: {collected.get('job_id')!r}")
    if job.base_sha and collected.get("base_sha") and collected["base_sha"] != job.base_sha:
        errs.append("base_sha mismatch")
    return errs


def result_from(collected: dict) -> Result:
    return Result.from_dict(collected)
