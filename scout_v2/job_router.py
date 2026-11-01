"""确定性 Backend Router（2026-10-31 用户裁定：两后端分工互不兜底）。

分工（configs/v2.yaml routing）：
- scan（bug hunter）→ 只走 Modal：临时沙箱隔离执行扫描，$30 额度护栏。
- repair/review/ci/reproduce → 只走 GitHub Actions（我们自己的仓库）：
  opencode 交互式 CLI 在 Modal 精简镜像里卡 bootstrap（上游已知问题），
  实测 repair 从未在 Modal 上成功过——兜底通道名存实亡，删掉以免"静默
  降级到一个跑不通的平台"。
- 任一后端不可用 → pause 并如实上报，绝不换平台重跑，也不静默降级。

保留的合规硬线：execution_scope=broad_scan 恒不上 Actions——GitHub ToS
禁止公共 runner 批量计算与所在仓库无关的第三方项目（计划 §1/§9）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .schemas import MODES

# 分工独占：每模式只有一条后端，没有 fallback。
DEFAULT_ROUTING = {
    "scan": {"primary": "modal", "fallback": ""},
    "reproduce": {"primary": "actions", "fallback": ""},
    "repair": {"primary": "actions", "fallback": ""},
    "review": {"primary": "actions", "fallback": ""},
    "ci": {"primary": "actions", "fallback": ""},
}


@dataclass
class RouteDecision:
    backend: str          # modal | github_actions | local | pause
    reason: str


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

        # scan 模式独占 Modal——不区分 scope。bug hunter 在 ToS 上与 GitHub
        # 托管 runner 无缘（§1/§9），self-hosted 又要抢 VPS 资源；Modal 的
        # 临时沙箱是唯一干净通道。Modal 不可用就暂停，不落 Actions。
        if mode == "scan":
            return self._route_modal_only(
                "scan runs on Modal only (ToS bars hosted runners; "
                "no Actions fallback by design)")

        primary, fallback = self.cfg.route_for(mode)
        chain = [b for b in (primary, fallback) if b]
        if not chain:
            return RouteDecision("pause", f"no backend configured for mode {mode!r}")
        last_reason = ""
        for backend in chain:
            ok, reason = self._try(backend, job)
            if ok:
                return RouteDecision(
                    "github_actions" if backend in ("github_actions", "actions") else backend,
                    reason)
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
