"""GitHub Actions 后端（P3 接入，P1 默认 dry-run）。

eligibility 规则（计划 §9）：
- execution_scope=broad_scan 一律 ineligible（Actions 不是免费扫描云）。
- 项目关联工作必须存在已启用的 workflow_dispatch 工作流；无 → ineligible。
- feature flag actions.enabled=false → ineligible。
"""

from __future__ import annotations


class BackendDisabledError(RuntimeError):
    pass


class ActionsBackend:
    def __init__(self, enabled: bool = False, workflow_exists: bool = False):
        self.enabled = enabled
        self.workflow_exists = workflow_exists

    def eligible(self, job: dict) -> bool:
        if not self.enabled:
            return False
        if job.get("execution_scope") == "broad_scan":
            return False
        if job.get("mode") not in {"repair", "review", "ci"}:
            return False
        return self.workflow_exists

    def submit(self, job: dict) -> str:
        if not self.eligible(job):
            raise BackendDisabledError("actions backend ineligible/disabled (P3 implements)")
        raise BackendDisabledError("Actions adapter not implemented until P3; dry-run only")

    def poll(self, remote_job_id: str) -> dict:
        raise BackendDisabledError("Actions adapter not implemented until P3")

    def cancel(self, remote_job_id: str) -> None:
        raise BackendDisabledError("Actions adapter not implemented until P3")

    def collect(self, remote_job_id: str) -> dict:
        raise BackendDisabledError("Actions adapter not implemented until P3")
