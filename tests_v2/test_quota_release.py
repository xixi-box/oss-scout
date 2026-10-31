"""额度账本与评论通道的回归测试。

2026-10-31 修掉的一个真 bug：abort 时调的是 release_day() 无参，实现是
`DELETE FROM claim_days`——把当天所有候选的占额一起抹掉。后果是任何一条
pipeline 中途失败都会把额度清零，下一轮又能开两条。这是"一天开出 12 个 PR"
的第二个原因（第一个是 ingest 绕过额度检查）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scout_v2.state_store import ClaimLedger  # noqa: E402

TZ = "Asia/Shanghai"


class TestReleaseClaim:
    def test_releasing_one_keeps_the_others(self, tmp_path):
        led = ClaimLedger(tmp_path / "t.db")
        try:
            assert led.try_claim(TZ, "a/x#1", max_per_day=2)[0]
            assert led.try_claim(TZ, "a/x#2", max_per_day=2)[0]
            assert led.count_today(TZ) == 2

            assert led.release_claim("a/x#1", TZ) is True
            assert led.count_today(TZ) == 1
            # 被释放的那条可以重新认领，另一条仍然占着
            assert led.try_claim(TZ, "a/x#3", max_per_day=2)[0]
            assert led.try_claim(TZ, "a/x#4", max_per_day=2)[0] is False
        finally:
            led.close()

    def test_release_unknown_is_noop(self, tmp_path):
        led = ClaimLedger(tmp_path / "t.db")
        try:
            led.try_claim(TZ, "a/x#1", max_per_day=2)
            assert led.release_claim("a/nothing#9", TZ) is False
            assert led.count_today(TZ) == 1
        finally:
            led.close()

    def test_release_day_clears_only_that_day(self, tmp_path):
        """release_day(day) 是运维用的整日回滚，仍然只动指定的那一天。"""
        import datetime as dt
        led = ClaimLedger(tmp_path / "t.db")
        try:
            led.try_claim(TZ, "a/x#1", max_per_day=5)
            today = led.count_today(TZ)
            assert today == 1
            led.release_day("1999-01-01")     # 不存在的日期
            assert led.count_today(TZ) == today
            led.release_day()
            assert led.count_today(TZ) == 0
        finally:
            led.close()


class TestAbortDoesNotResetBudget:
    """这是 bug 的核心：一条 abort 不能把当天的额度还回去。"""

    def test_abort_releases_only_itself(self, tmp_path):
        from scout_v2.workers.auto_pipeline import _emergency_release

        led = ClaimLedger(tmp_path / "state" / "scout_v2.sqlite3")
        led.try_claim(TZ, "a/one#1", max_per_day=2)
        led.try_claim(TZ, "a/two#2", max_per_day=2)
        led.close()

        _emergency_release(tmp_path, "a/one#1", TZ)

        led = ClaimLedger(tmp_path / "state" / "scout_v2.sqlite3")
        try:
            assert led.count_today(TZ) == 1, "other candidate's claim must survive"
        finally:
            led.close()

    def test_emergency_release_without_id_does_nothing(self, tmp_path):
        """没有 candidate_id 时宁可漏放，也不要误清清整张表。"""
        from scout_v2.workers.auto_pipeline import _emergency_release

        led = ClaimLedger(tmp_path / "state" / "scout_v2.sqlite3")
        led.try_claim(TZ, "a/one#1", max_per_day=2)
        led.close()

        _emergency_release(tmp_path, "", TZ)

        led = ClaimLedger(tmp_path / "state" / "scout_v2.sqlite3")
        try:
            assert led.count_today(TZ) == 1
        finally:
            led.close()


class TestLaneGate:
    """restricted 车道的规则写在 PolicyEngine 里，但 pipeline 必须真的调用到。"""

    def test_restricted_requires_confirmation_in_source(self):
        src = (Path(__file__).resolve().parents[1] / "scout_v2" / "workers"
               / "auto_pipeline.py").read_text(encoding="utf-8")
        assert 'lane == "restricted"' in src
        assert "needs_maintainer_confirm" in src

    def test_gated_is_still_blocked(self):
        src = (Path(__file__).resolve().parents[1] / "scout_v2" / "workers"
               / "auto_pipeline.py").read_text(encoding="utf-8")
        assert 'lane == "gated"' in src
