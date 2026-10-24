"""Modal 后端（P2 接入，P1 默认 dry-run）。

P1 裁定：BudgetGuard.fail-closed 未通过或 modal.enabled=false 时 submit() 直接拒绝，
不创建任何 Sandbox（计划 §12：费用保护必须先于任何实际运行上线）。
"""

from __future__ import annotations

from ..budget import BudgetGuard


class BackendDisabledError(RuntimeError):
    pass


class BudgetExceededError(RuntimeError):
    pass


class ModalBackend:
    def __init__(self, budget: BudgetGuard):
        self.budget = budget

    def eligible(self, job: dict) -> bool:
        ok, _ = self.budget.can_start(job.get("max_estimated_compute_usd", 0.0))
        return ok

    def submit(self, job: dict) -> str:
        if not self.budget.enabled:
            raise BackendDisabledError("modal backend disabled by feature flag")
        ok, reason = self.budget.can_start(job.get("max_estimated_compute_usd", 0.0))
        if not ok:
            raise BudgetExceededError(reason)
        raise BackendDisabledError("Modal adapter not implemented until P2; dry-run only")

    def poll(self, remote_job_id: str) -> dict:
        raise BackendDisabledError("Modal adapter not implemented until P2")

    def cancel(self, remote_job_id: str) -> None:
        raise BackendDisabledError("Modal adapter not implemented until P2")

    def collect(self, remote_job_id: str) -> dict:
        raise BackendDisabledError("Modal adapter not implemented until P2")
