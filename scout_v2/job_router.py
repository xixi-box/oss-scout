"""Route project-contribution work exclusively to GitHub Actions; retired scans are blocked."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .schemas import MODES

# 分工独占：每模式只有一条后端，没有 fallback。
DEFAULT_ROUTING = {
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
    actions_enabled: bool = False
    actions_eligible: bool = False    # Actions Adapter.eligible(job) 的结论
    routing: dict[str, dict[str, str]] | None = None

    def route_for(self, mode: str) -> tuple[str, str]:
        table = self.routing or DEFAULT_ROUTING
        entry = table.get(mode) or DEFAULT_ROUTING.get(mode) or \
            {"primary": "", "fallback": ""}
        return entry.get("primary", ""), entry.get("fallback", "")


class JobRouter:
    def __init__(self, config: RouterConfig):
        self.cfg = config

    def route(self, job: dict[str, Any]) -> RouteDecision:
        mode = job.get("mode", "")
        scope = job.get("execution_scope", "project_contribution")
        if mode not in MODES:
            return RouteDecision("pause", f"invalid mode {mode!r}")
        if scope != "project_contribution":
            return RouteDecision("pause", "broad scanning is retired")

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
        if backend in ("github_actions", "actions"):
            if not self.cfg.actions_enabled:
                return False, "Actions disabled by feature flag"
            if not self.cfg.actions_eligible:
                return False, "Actions ineligible (no compliant workflow / quota risk)"
            return True, "Actions ok"
        if backend == "local":
            return True, "local ok"
        return False, f"unknown backend {backend!r}"
