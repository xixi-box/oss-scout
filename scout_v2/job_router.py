"""确定性 Backend Router（计划 §5）：规则表驱动，不给 Agent 自主决策。

路由矩阵（mode × execution_scope → 首选/备用）：
- scan / 跨仓库 broad_scan → 只走 Modal；Actions 永远拒绝。
- reproduce（未进入贡献流程）→ Modal；不能为了绕过 Actions 限制批量 Fork。
- repair / review / ci（project_contribution）→ Actions 优先；Actions 不可用回退 Modal
  （仅预算内），再不行暂停。
回退仅限平台故障/配额/资源问题，绝不因测试断言失败换平台重跑。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .schemas import MODES


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
    actions_eligible: bool = False    # Actions Adapter.eligible(job) 的结论（有合规 workflow 等）
    modal_platform_ok: bool = True    # 平台可用性（区别于预算）


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
                                 "broad_scan only applies to mode=scan (policy: no mass fork/repro on Actions)")

        if scope == "broad_scan" or mode == "scan":
            return self._route_modal_only(
                "broad/scan workloads never run on GitHub Actions (§5)")

        if mode == "reproduce":
            return self._route_modal_only(
                "pre-contribution reproduce runs on Modal; no mass-fork to reach Actions")

        # repair / review / ci：project_contribution，Actions 优先
        if scope != "project_contribution":
            return RouteDecision("pause", f"unknown scope {scope!r}")
        if self.cfg.actions_enabled and self.cfg.actions_eligible:
            return RouteDecision("github_actions", "contribution work: Actions first")
        if self.cfg.actions_enabled and not self.cfg.actions_eligible:
            if self._modal_available():
                return RouteDecision("modal", "Actions ineligible → Modal fallback (budget-checked)",
                                     fallback_used=True, ineligible=True)
            return RouteDecision("pause",
                                 "Actions ineligible and Modal unavailable/budget-blocked → pause")
        if self.cfg.actions_enabled is False:
            # feature flag 关闭时（P1 默认）：本地浅跑或暂停由调用方决定，路由给 local
            return RouteDecision("local", "actions backend disabled by feature flag (P1 default)")
        return RouteDecision("pause", "no eligible backend")

    def _route_modal_only(self, why: str) -> RouteDecision:
        if self.cfg.modal_enabled and self.cfg.modal_budget_ok and self.cfg.modal_platform_ok:
            return RouteDecision("modal", why)
        if self.cfg.modal_enabled and not self.cfg.modal_budget_ok:
            return RouteDecision("pause", f"{why}; but Modal budget blocked → pause (never Actions)")
        return RouteDecision("pause", f"{why}; but Modal disabled → pause")

    def _modal_available(self) -> bool:
        return (self.cfg.modal_enabled and self.cfg.modal_budget_ok and self.cfg.modal_platform_ok)
