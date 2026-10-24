"""Modal 预算守门测试（计划 §16：软阈值/账单不可用/硬上限未确认 → 不创建 Sandbox）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scout_v2.adapters.modal_backend import (BudgetExceededError,  # noqa: E402
                                             ModalBackend)
from scout_v2.budget import BudgetGuard, DisabledBilling  # noqa: E402


class FakeBilling:
    def __init__(self, metered: float | None, healthy: bool = True):
        self._m, self._h = metered, healthy

    def metered_usage_usd(self) -> float | None:
        return self._m

    def healthy(self) -> bool:
        return self._h


def guard(metered, cap=25.0, enabled=True, healthy=True, reserved=0.0) -> BudgetGuard:
    return BudgetGuard(soft_cap_usd=cap, enabled=enabled,
                       billing=FakeBilling(metered, healthy), reserved_usd=reserved)


class TestFailClosed:
    def test_disabled_by_feature_flag(self):
        ok, reason = guard(None, enabled=False).can_start(0.1)
        assert not ok and "disabled" in reason

    def test_billing_unavailable(self):
        ok, reason = guard(None).can_start(0.1)
        assert not ok and "fail-closed" in reason

    def test_billing_unhealthy(self):
        ok, _ = guard(10.0, healthy=False).can_start(0.1)
        assert not ok


class TestSoftCap:
    def test_within_cap_allowed(self):
        ok, _ = guard(20.0).can_start(4.0)
        assert ok

    def test_over_cap_refused(self):
        ok, reason = guard(24.0).can_start(2.0)
        assert not ok and "soft cap" in reason

    def test_reservation_counts_toward_cap(self):
        ok, _ = guard(20.0, reserved=4.0).can_start(2.0)
        assert not ok

    def test_concurrency_limit(self):
        g = guard(0.0)
        g.max_concurrent = 1
        g.reserve(1.0)
        ok, reason = g.can_start(1.0)
        assert not ok and "concurrency" in reason
        g.release(1.0)
        assert g.can_start(1.0)[0]

    def test_estimate_upper_bound(self):
        # 30 分钟 × 2 CPU × $0.5/cpu-h = $0.5
        assert BudgetGuard.estimate_upper_bound_usd(30, 2, 0.5) == pytest_approx(0.5)


def pytest_approx(x):
    import pytest
    return pytest.approx(x)


class TestModalAdapter:
    def test_submit_refused_over_budget(self):
        b = ModalBackend(guard(24.9))
        import pytest
        with pytest.raises(BudgetExceededError):
            b.submit({"max_estimated_compute_usd": 1.0})

    def test_submit_refused_when_disabled(self):
        from scout_v2.adapters.modal_backend import BackendDisabledError
        b = ModalBackend(guard(0.0, enabled=False))
        import pytest
        with pytest.raises(BackendDisabledError):
            b.submit({"max_estimated_compute_usd": 0.0})
