"""调度（计划 §14）：P1 只提供配置驱动的调度计划与守门，不实现常驻进程。

守门点：v2.yaml 的节奏若与 Playbook §1.5 冲突（观测 <2h、连续消费开启），
`scheduled_plan()` 会拒绝输出并要求人工修订 Playbook —— 计划文本允许激进节奏，
但 Playbook 优先，切换必须人工完成。
"""
from __future__ import annotations

from dataclasses import dataclass


class ScheduleConflictError(RuntimeError):
    pass


@dataclass
class ScheduledTask:
    name: str
    interval_minutes: int
    resource: str


def scheduled_plan(v2_cfg: dict) -> list[ScheduledTask]:
    sched = (v2_cfg.get("scheduler") or {})
    observe = int(sched.get("observe_interval_minutes", 120))
    continuous = bool(v2_cfg.get("continuous_consume", False))

    # Playbook §1.5：观测任务每 2 小时，禁止消费/评论/开 PR；消费仅三窗
    if observe < 120:
        raise ScheduleConflictError(
            f"observe_interval_minutes={observe} < Playbook's 120min — revise AGENTS.md first")
    if continuous:
        raise ScheduleConflictError(
            "continuous_consume=true requires Playbook migration (P6 + human review)")

    windows = sched.get("consume_windows_local") or ["10:30", "16:30", "22:30"]
    plan = [
        ScheduledTask("observe_discover", observe, "VPS API (discover.py only)"),
        ScheduledTask("consume_window", 0, f"windows {windows} (pr_monitor → discover → consume)"),
    ]
    if int(sched.get("health_refresh_after_days", 7)) > 0:
        plan.append(ScheduledTask("health_refresh",
                                  int(sched["health_refresh_after_days"]) * 24 * 60,
                                  "health_check.py"))
    return plan
