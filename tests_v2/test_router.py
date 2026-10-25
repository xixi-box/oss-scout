"""路由测试（计划 §5 矩阵 + §16：broad_scan 拒 Actions、回退仅限平台问题）。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scout_v2.job_router import JobRouter, RouterConfig  # noqa: E402


def cfg(**kw) -> RouterConfig:
    base = dict(modal_enabled=False, actions_enabled=False, modal_budget_ok=False,
                actions_eligible=False, modal_platform_ok=True)
    base.update(kw)
    return RouterConfig(**base)


def job(mode, scope="project_contribution") -> dict:
    return {"mode": mode, "execution_scope": scope, "base_sha": "abc"}


class TestBroadScan:
    @pytest.mark.parametrize("modal,actions", [(True, True), (False, True)])
    def test_broad_scan_never_actions(self, modal, actions):
        r = JobRouter(cfg(modal_enabled=modal, modal_budget_ok=modal,
                          actions_enabled=actions, actions_eligible=actions))
        d = r.route(job("scan", "broad_scan"))
        assert d.backend != "github_actions"

    def test_broad_scan_pauses_when_modal_blocked(self):
        d = JobRouter(cfg(modal_enabled=True, modal_budget_ok=False)).route(
            job("scan", "broad_scan"))
        assert d.backend == "pause" and "budget" in d.reason

    def test_broad_scan_modal_ok(self):
        d = JobRouter(cfg(modal_enabled=True, modal_budget_ok=True)).route(
            job("scan", "broad_scan"))
        assert d.backend == "modal"

    def test_broad_scan_only_mode_scan(self):
        d = JobRouter(cfg(modal_enabled=True, modal_budget_ok=True)).route(
            job("reproduce", "broad_scan"))
        assert d.backend == "pause"


class TestReproduce:
    def test_reproduce_modal_primary(self):
        r = JobRouter(cfg(actions_enabled=True, actions_eligible=True,
                          modal_enabled=True, modal_budget_ok=True))
        d = r.route(job("reproduce"))
        assert d.backend == "modal" and not d.fallback_used

    def test_reproduce_falls_back_to_actions(self):
        """能力对称裁定：reproduce 主选 Modal 不可用时回退 Actions（project_contribution）。"""
        r = JobRouter(cfg(actions_enabled=True, actions_eligible=True,
                          modal_enabled=True, modal_budget_ok=False))
        d = r.route(job("reproduce"))
        assert d.backend == "github_actions" and d.fallback_used

    def test_repair_falls_back_to_modal(self):
        """对称：repair 主选 Actions 不可用回退 Modal（原方向的反向）。"""
        r = JobRouter(cfg(actions_enabled=True, actions_eligible=False,
                          modal_enabled=True, modal_budget_ok=True))
        d = r.route(job("repair"))
        assert d.backend == "modal" and d.fallback_used and d.ineligible

    def test_pauses_when_both_blocked(self):
        r = JobRouter(cfg(actions_enabled=True, actions_eligible=False,
                          modal_enabled=True, modal_budget_ok=False))
        d = r.route(job("ci"))
        assert d.backend == "pause"

    def test_p1_default_local(self):
        # P1：所有 feature flag 关闭 → local（骨架），绝不 Actions
        d = JobRouter(cfg()).route(job("review"))
        assert d.backend == "local"

    def test_invalid_mode_rejected(self):
        d = JobRouter(cfg()).route({"mode": "yolo"})
        assert d.backend == "pause" and "invalid mode" in d.reason

    def test_scan_on_actions_when_modal_blocked(self):
        """对称：scan（project_contribution）可走 Actions；broad_scan 恒拒。"""
        r = JobRouter(cfg(actions_enabled=True, actions_eligible=True,
                          modal_enabled=True, modal_budget_ok=False))
        d = r.route(job("scan", "project_contribution"))
        assert d.backend == "github_actions" and d.fallback_used
