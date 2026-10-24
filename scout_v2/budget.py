"""Modal 费用保护（计划 §12）：预算守门是硬闸，先于任何 Sandbox 创建。

fail-closed：账单数据不可读、API 故障、费用不可解释 → 拒绝创建。
估算上界 = 已计量 + 并发已预留 + 新任务上界 ≤ 月软阈值。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class BillingSource(Protocol):
    """账单数据源协议。真实实现包 `modal billing summary --json` / SDK；测试用 fake。"""

    def metered_usage_usd(self) -> float | None: ...
    def healthy(self) -> bool: ...


class DisabledBilling:
    """未配置账单源（modal.enabled=false 或 P2 未启用）：一律拒绝创建。"""

    def metered_usage_usd(self) -> float | None:
        return None

    def healthy(self) -> bool:
        return False


@dataclass
class BudgetGuard:
    soft_cap_usd: float
    enabled: bool = False
    billing: BillingSource = field(default_factory=DisabledBilling)
    reserved_usd: float = 0.0        # 并发进行中任务的已承诺上界
    max_concurrent: int = 1
    _in_flight: int = 0

    def can_start(self, max_estimated_compute_usd: float) -> tuple[bool, str]:
        """创建 Sandbox 前调用。返回 (allowed, reason)。"""
        if not self.enabled:
            return False, "modal backend disabled by feature flag"
        if not self.billing.healthy():
            return False, "billing source unavailable — fail-closed, no sandbox"
        metered = self.billing.metered_usage_usd()
        if metered is None:
            return False, "metered usage unreadable — fail-closed, no sandbox"
        total = metered + self.reserved_usd + max_estimated_compute_usd
        if total > self.soft_cap_usd:
            return (False,
                    f"soft cap exceeded: metered {metered:.2f} + reserved {self.reserved_usd:.2f}"
                    f" + new {max_estimated_compute_usd:.2f} > cap {self.soft_cap_usd:.2f}"
                    " — pause broad_scan/reproduce until quota resets")
        if self._in_flight >= self.max_concurrent:
            return False, f"concurrency limit reached ({self._in_flight}/{self.max_concurrent})"
        return True, "ok"

    def reserve(self, amount_usd: float) -> None:
        self.reserved_usd += amount_usd
        self._in_flight += 1

    def release(self, amount_usd: float) -> None:
        self.reserved_usd = max(0.0, self.reserved_usd - amount_usd)
        self._in_flight = max(0, self._in_flight - 1)

    @staticmethod
    def estimate_upper_bound_usd(max_runtime_minutes: int, cpu: int,
                                 usd_per_cpu_hour: float) -> float:
        """预留上界 = 运行时长 × 资源费率（计划 §12 estimated_max_cost）。"""
        return (max_runtime_minutes / 60.0) * cpu * usd_per_cpu_hour
