"""确定性 Backend Router（计划 §5；用户裁定 2026-10-25：两后端能力全对称）。

路由策略表（configs/v2.yaml routing）：每模式 {primary, fallback}，两条后端都可承载
全部五种模式（同一份 worker 代码）。回退仅限平台故障/配额/资格问题，绝不因测试
断言失败换平台重跑。

唯一保留的不对称是合规线：execution_scope=broad_scan 恒不上 Actions——
GitHub ToS 禁止公共 runner 批量计算与所在仓库无关的第三方项目（计划 §1/§9），
这是策略拒绝而非能力缺失（scan/reproduce 在 project_contribution 范围内
可走 Actions，与 Modal 同权）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .schemas import MODES

DEFAULT_ROUTING = {
    "scan": {"primary": "modal", "fallback": "actions"},
    "reproduce": {"primary": "actions", "fallback": "modal"},
    "repair": {"primary": "actions", "fallback": "modal"},
    "review": {"primary": "actions", "fallback": "modal"},
    "ci": {"primary": "actions", "fallback": "modal"},
}


@dataclass
class RouteDecision:
    backend: str          # modal | github_actions | local | pause
    reason: str
    fallback_used: bool = False
    ineligible: bool = False   # 首选明确不可用（Actions 无合规工作流等）


@dataclass
class RouterConfig:
    modal_enabled: bool = False
    actions_enabled: bool = False
    modal_budget_ok: bool = False     # BudgetGuard.can_start() 的结论
    actions_eligible: bool = False    # Actions Adapter.eligible(job) 的结论
    modal_platform_ok: bool = True
    routing: dict[str, dict[str, str]] | None = None

    def route_for(self, mode: str) -> tuple[str, str]:
        table = self.routing or DEFAULT_ROUTING
        entry = table.get(mode) or DEFAULT_ROUTING.get(mode) or \
            {"primary": "modal", "fallback": "actions"}
        return entry.get("primary", "modal"), entry.get("fallback", "actions")


class JobRouter:
    def __init__(self, config: RouterConfig):
        self.cfg = config

    def route(self, job: dict[str, Any]) -> RouteDecision:
        mode = job.get("mode", "")
        scope = job.get("execution_scope", "project_contribution")
        if mode not in MODES:
            return RouteDecision("pause", f"invalid mode {mode!r}")
        if scope == "broad_scan" and mode != "scan":
            return RouteDecision("pause",
                                 "broad_scan only applies to mode=scan")

        # 合规硬线：broad_scan 恒不上 Actions（ToS），仅 Modal 可承载；Modal 不可用即暂停
        if scope == "broad_scan":
            return self._route_modal_only(
                "broad_scan never runs on GitHub Actions (ToS §1/§9)")

        primary, fallback = self.cfg.route_for(mode)
        chain = [b for b in (primary, fallback) if b]
        last_reason = ""
        used_fallback = False
        for i, backend in enumerate(chain):
            ok, reason = self._try(backend, job)
            if ok:
                return RouteDecision(
                    "github_actions" if backend in ("github_actions", "actions") else backend,
                    reason,
                    fallback_used=(i > 0),
                    ineligible=(i > 0))   # 首选被跳过 ⇒ 标记，控制器可审计回退原因
            used_fallback = used_fallback or i > 0
            last_reason = reason
        # P1 默认：仅当主选与备选都被 feature flag 关闭时，退到本地骨架执行
        if "disabled by feature flag" in last_reason and \
                all("disabled by feature flag" in self._try(b, job)[1] for b in chain):
            return RouteDecision("local", "all backends disabled by feature flag (P1 default)")
        return RouteDecision("pause", last_reason or "no eligible backend")

    def _try(self, backend: str, job: dict[str, Any]) -> tuple[bool, str]:
        if backend == "modal":
            if not self.cfg.modal_enabled:
                return False, "Modal disabled by feature flag"
            if not self.cfg.modal_platform_ok:
                return False, "Modal platform unavailable"
            if not self.cfg.modal_budget_ok:
                return False, "Modal budget blocked (fail-closed)"
            return True, "Modal ok"
        if backend in ("github_actions", "actions"):
            if not self.cfg.actions_enabled:
                return False, "Actions disabled by feature flag"
            if not self.cfg.actions_eligible:
                return False, "Actions ineligible (no compliant workflow / quota risk)"
            return True, "Actions ok"
        if backend == "local":
            return True, "local ok"
        return False, f"unknown backend {backend!r}"

    def _route_modal_only(self, why: str) -> RouteDecision:
        if self.cfg.modal_enabled and self.cfg.modal_budget_ok and self.cfg.modal_platform_ok:
            return RouteDecision("modal", why)
        if self.cfg.modal_enabled and not self.cfg.modal_budget_ok:
            return RouteDecision("pause", f"{why}; but Modal budget blocked → pause (never Actions)")
        return RouteDecision("pause", f"{why}; but Modal disabled → pause")
