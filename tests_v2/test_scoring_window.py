"""选品策略（2026-10-28 校准）的回归测试。

实测：一行修复的 bug 在 issue 开出 1.5 分钟内就有人提 PR（#6289 的 PR 比
issue 晚 1 分半），所以"越新越好"是错的。真正的机会窗口是热情期已过、
还没人接手的 2h–48h。
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import discover as d  # noqa: E402


def _sig(**kw):
    base = {"maintainer_confirm": False, "reactions": 0, "labels": ["bug"],
            "repro_signal": False, "comments": 0, "cross_ref_pr": False,
            "claim_signal": False, "body_len": 900}
    base.update(kw)
    return base


def _iso(hours_ago: float) -> str:
    t = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours_ago)
    return t.isoformat().replace("+00:00", "Z")


class TestOpportunityWindow:
    def test_very_fresh_issue_is_not_top_grade(self):
        """<2h 是内部人主场，不应评 A。"""
        s, cls, _ = d.score("div returns wrong value", _sig(), _iso(1), "open")
        assert cls != "A"

    def test_window_issue_reaches_a(self):
        for hours in (10, 30):
            _, cls, _ = d.score("div returns wrong value", _sig(), _iso(hours), "open")
            assert cls == "A", f"{hours}h should be A"

    def test_old_issue_is_mid_grade(self):
        """龄太久的方案可能已跑偏，只是仍有价值。"""
        s_new, _ = d.score("x", _sig(), _iso(10), "open")[:2]
        s_old, _ = d.score("x", _sig(), _iso(24 * 20), "open")[:2]
        assert s_old < s_new

    def test_ready_for_dev_boosted(self):
        """维护者已确认可动手，根因描述通常已定位到代码。"""
        s_plain, cls_plain, _ = d.score("x", _sig(), _iso(10), "open")
        s_rfd, cls_rfd, _ = d.score("x", _sig(labels=["bug", "ready-for-dev"]),
                                    _iso(10), "open")
        assert s_rfd > s_plain
        assert cls_rfd == "A"

    def test_priority_high_demoted(self):
        """高优 issue 通常有核心成员盯着。"""
        s_hi, _, _ = d.score("x", _sig(labels=["bug", "priority:high"]),
                             _iso(10), "open")
        s_normal, _, _ = d.score("x", _sig(), _iso(10), "open")
        assert s_hi < s_normal
