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


class ModalCliBilling:
    """从 `modal billing summary --json` 读当月计量成本（计划 §12）。

    防御性解析：官方返回结构未实测锁定，因此只认明确像成本的数值字段；
    解析不出 → 返回 None → BudgetGuard fail-closed 拒绝创建（宁可停也不超支）。
    """

    COMMAND = ("modal", "billing", "summary", "--json")

    def __init__(self, runner=None):
        self._runner = runner or self._run_cli

    @staticmethod
    def _run_cli(cmd: tuple[str, ...]) -> tuple[int, str]:
        import subprocess
        p = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30)
        return p.returncode, p.stdout

    def healthy(self) -> bool:
        try:
            rc, out = self._runner(self.COMMAND)
        except Exception:  # noqa: BLE001 — CLI 缺失/超时/任何异常都视为不健康
            return False
        return rc == 0 and bool(out.strip())

    def metered_usage_usd(self) -> float | None:
        try:
            rc, out = self._runner(self.COMMAND)
            if rc != 0:
                return None
            import json
            data = json.loads(out)
        except Exception:  # noqa: BLE001
            return None
        return self._extract_usd(data)

    @staticmethod
    def _extract_usd(data) -> float | None:
        """在（可能嵌套的）账单 JSON 里找明确的成本字段；找不到 = None（fail-closed）。"""
        import numbers
        COST_HINTS = ("metered_cost", "total_cost", "cost", "charges", "spend", "usage_usd")
        if isinstance(data, dict):
            for k, v in data.items():
                kl = str(k).lower()
                if isinstance(v, numbers.Number) and not isinstance(v, bool) \
                        and any(h in kl for h in COST_HINTS):
                    return float(v)
            for v in data.values():
                found = ModalCliBilling._extract_usd(v)
                if found is not None:
                    return found
        elif isinstance(data, list):
            for item in data:
                found = ModalCliBilling._extract_usd(item)
                if found is not None:
                    return found
        return None


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
