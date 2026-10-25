"""本地后端：测试与 P1 骨架执行用；不在 VPS 上跑重活。"""

from __future__ import annotations

from pathlib import Path

from ..schemas import Job
from ..workers.core import execute


class LocalBackend:
    def __init__(self, workdir: Path):
        self.workdir = workdir

    def eligible(self, job: dict) -> bool:
        return True

    def submit(self, job: dict) -> str:
        j = Job.from_dict(job)
        errs = j.validate()
        if errs:
            raise ValueError(f"invalid job: {errs}")
        out = self.workdir / f"artifacts/{j.job_id}"
        result = execute(j, out, backend="local")
        (out / "result.json").write_text(
            __import__("json").dumps(result.to_dict(), ensure_ascii=False, indent=1),
            encoding="utf-8")
        return j.job_id

    def poll(self, remote_job_id: str) -> dict:
        return {"job_id": remote_job_id, "state": "succeeded"}

    def cancel(self, remote_job_id: str) -> None:
        pass  # 本地同步执行，无进程可取消

    def collect(self, remote_job_id: str) -> dict:
        f = self.workdir / f"artifacts/{remote_job_id}/result.json"
        if not f.is_file():
            raise FileNotFoundError(f"no result for {remote_job_id}")
        import json
        return json.loads(f.read_text(encoding="utf-8"))
