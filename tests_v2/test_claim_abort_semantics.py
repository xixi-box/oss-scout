"""abort 后的账本语义（2026-11-06 实录）：

release_claim 不再 DELETE——abort 记录留在表里，一石二鸟：
  1. try_claim 的同日闸继续挡住同一条候选当天被二次认领
     （队列恢复把 aborted 候选重置回 new 后，RuVector#1292 / OpenViking#6823
     各开出两个重复 PR，就是这道闸被 DELETE 拆掉的结果）；
  2. abort 记录不占当日额度，失败仍然让下一条候选顶上。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scout_v2.state_store import ClaimLedger  # noqa: E402


def test_aborted_claim_blocks_same_day_reclaim(tmp_path):
    led = ClaimLedger(tmp_path / "t.db")
    ok, _ = led.try_claim("Asia/Shanghai", "repo/one#1", max_per_day=5)
    assert ok
    led.release_claim("repo/one#1")
    ok2, reason = led.try_claim("Asia/Shanghai", "repo/one#1", max_per_day=5)
    assert not ok2
    assert "already claimed" in reason
    led.close()


def test_aborted_claims_do_not_consume_daily_budget(tmp_path):
    led = ClaimLedger(tmp_path / "t.db")
    for i in range(3):
        key = f"repo/one#{i}"
        assert led.try_claim("Asia/Shanghai", key, max_per_day=2)[0]
        led.release_claim(key)          # 全部失败退坑
    # 三次失败只留下 abort 记录，额度应当还是空的：第 4 条照样能认领。
    ok, reason = led.try_claim("Asia/Shanghai", "repo/one#99", max_per_day=2)
    assert ok, reason
    assert led.count_today("Asia/Shanghai") == 1   # 只数成功的这条
    led.close()


def test_success_keeps_consuming_budget(tmp_path):
    led = ClaimLedger(tmp_path / "t.db")
    assert led.try_claim("Asia/Shanghai", "repo/one#1", max_per_day=2)[0]
    assert led.try_claim("Asia/Shanghai", "repo/one#2", max_per_day=2)[0]
    ok, reason = led.try_claim("Asia/Shanghai", "repo/one#3", max_per_day=2)
    assert not ok
    assert "daily limit 2" in reason
    assert led.count_today("Asia/Shanghai") == 2
    led.close()
