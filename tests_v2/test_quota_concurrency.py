"""并发认领与状态机测试（计划 §16：双进程竞争当日至多一条新认领）。"""
from __future__ import annotations

import json
import multiprocessing
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scout_v2.state_store import (BlockerRegistry, ClaimLedger, JobStore,  # noqa: E402
                                  Worklog, atomic_json_write, business_day)

TZ = "Asia/Shanghai"


def _worker(db_path: str, q):
    """独立进程：尝试占当日名额，返回结果。"""
    ledger = ClaimLedger(Path(db_path))
    ok, reason = ledger.try_claim(TZ, "aaif-goose/goose#1")
    q.put((ok, reason))
    ledger.close()


class TestClaimLedgerConcurrency:
    def test_two_processes_only_one_claims(self, tmp_path):
        """同一候选被多进程同时认领时，只能有一个成功。

        注意这是同一 cid 的重复认领，与每日条数上限无关 —— 即使
        max_per_day=0（不限）也必须拒绝第二个。

        全量跑时偶尔 flaky：4 个进程 join 后队列里的结果可能还没全部
        落盘。加一次 drain 等待，并断言"恰好 1 个成功"而非只看数量。
        """
        db = tmp_path / "ledger.sqlite3"
        ledger = ClaimLedger(db)
        ledger.close()
        q = multiprocessing.Queue()
        procs = [multiprocessing.Process(target=_worker, args=(str(db), q))
                 for _ in range(4)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=30)
        results = []
        for _ in procs:
            results.append(q.get(timeout=30))
        winners = [why for ok, why in results if ok]
        assert len(winners) == 1, f"expected exactly 1 winner, got {len(winners)}: {results}"

    def test_release_allows_reclaim(self, tmp_path):
        """每日上限可配置；同一候选重复认领永远拒绝。"""
        ledger = ClaimLedger(tmp_path / "l.sqlite3")
        ok, _ = ledger.try_claim(TZ, "a#1", max_per_day=1)
        assert ok
        ok2, reason = ledger.try_claim(TZ, "a#2", max_per_day=1)
        assert not ok2 and "daily limit" in reason
        ledger.release_day()
        ok3, _ = ledger.try_claim(TZ, "a#3", max_per_day=1)
        assert ok3
        ledger.close()

    def test_same_candidate_cannot_be_claimed_twice(self, tmp_path):
        ledger = ClaimLedger(tmp_path / "l.sqlite3")
        assert ledger.try_claim(TZ, "a#1", max_per_day=0)[0] is True
        again, reason = ledger.try_claim(TZ, "a#1", max_per_day=0)
        assert again is False and "already claimed" in reason
        ledger.close()

    def test_unlimited_allows_many(self, tmp_path):
        """max_per_day=0 表示不限条数（2026-10-30 用户裁定）。"""
        ledger = ClaimLedger(tmp_path / "l.sqlite3")
        for i in range(5):
            ok, why = ledger.try_claim(TZ, f"a#{i}", max_per_day=0)
            assert ok, why
        ledger.close()


class TestBusinessDay:
    def test_naive_ts_untouched(self):
        assert business_day(__import__("datetime").datetime(2026, 10, 24, 23, 0), TZ) == "2026-10-24"

    def test_utc_ts_converted(self):
        import datetime as dt
        from zoneinfo import ZoneInfo
        ts = dt.datetime(2026, 10, 24, 17, 0, tzinfo=dt.timezone.utc)  # 北京时间 25 日 01:00
        assert business_day(ts, TZ) == "2026-10-25"


class TestWorklogCompat:
    def test_read_real_worklog_with_bad_lines(self, tmp_path):
        from legacy_samples import LEGACY_WORKLOG_LINES
        p = tmp_path / "worklog.jsonl"
        p.write_text("\n".join(LEGACY_WORKLOG_LINES) + "\n", encoding="utf-8")
        wl = Worklog(p)
        recs = wl.read_all()
        assert len(recs) == 4  # 坏行被跳过，好行全保留

    def test_claimed_today_over_real_window(self, tmp_path):
        from legacy_samples import LEGACY_WORKLOG_LINES
        p = tmp_path / "worklog.jsonl"
        p.write_text("\n".join(LEGACY_WORKLOG_LINES) + "\n", encoding="utf-8")
        wl = Worklog(p)
        hits = wl.claimed_today(TZ)  # 样本里没有当日 claim
        assert hits == []


class TestAtomicWrite:
    def test_write_roundtrip_and_bak(self, tmp_path):
        p = tmp_path / "q" / "candidates.json"
        atomic_json_write(p, {"a": 1})
        atomic_json_write(p, {"a": 2})
        assert json.loads(p.read_text(encoding="utf-8")) == {"a": 2}
        assert json.loads(p.with_suffix(".json.bak").read_text(encoding="utf-8")) == {"a": 1}

    def test_verify_catches_corruption(self, tmp_path, monkeypatch):
        """0x00 场景：replace 后文件被填零 → 落地后校验必须抛异常，
        且 .bak 保留上一版好内容供恢复。"""
        import os
        p = tmp_path / "x.json"
        atomic_json_write(p, {"a": 1})                      # 第一版落地 + .bak 生成前无旧版
        atomic_json_write(p, {"b": 2})                      # .bak = {"a":1}
        real_replace = os.replace

        def bad_replace(src, dst):
            Path(dst).write_bytes(b"\x00\x00\x00")

        monkeypatch.setattr(os, "replace", bad_replace)
        with pytest.raises(Exception):
            atomic_json_write(p, {"c": 3})
        monkeypatch.setattr(os, "replace", real_replace)
        assert p.read_bytes() == b"\x00\x00\x00"            # 主文件确已损坏
        assert json.loads(p.with_suffix(".json.bak").read_text(encoding="utf-8")) == {"b": 2}


class TestJobStore:
    def test_idempotent_submit(self, tmp_path):
        js = JobStore(tmp_path / "jobs.sqlite3")
        job = {"job_id": "job-1", "idempotency_key": "verify:r:sha:fp",
               "mode": "reproduce", "base_sha": "abc"}
        jid, created1 = js.submit(job)
        jid2, created2 = js.submit(dict(job))
        assert created1 and not created2 and jid == jid2

    def test_state_machine_and_terminal_idempotency(self, tmp_path):
        js = JobStore(tmp_path / "jobs.sqlite3")
        js.submit({"job_id": "job-1", "idempotency_key": "k1", "mode": "scan"})
        assert js.transition("job-1", "leased", lease_seconds=60)
        assert js.transition("job-1", "running")
        assert js.transition("job-1", "succeeded")
        assert js.transition("job-1", "succeeded")          # 终态幂等
        assert not js.transition("job-1", "failed")          # 不允许改终态

    def test_expired_lease(self, tmp_path):
        js = JobStore(tmp_path / "jobs.sqlite3")
        js.submit({"job_id": "job-1", "idempotency_key": "k1", "mode": "scan"})
        js.transition("job-1", "leased", lease_seconds=-1)   # 已过期
        assert js.expired_leases() == ["job-1"]


class TestBlockers:
    def test_register_and_check(self, tmp_path):
        reg = BlockerRegistry(tmp_path / "b.sqlite3")
        assert not reg.is_blocked("r:push")
        reg.register("r:push", "403")
        assert reg.is_blocked("r:push") and reg.reason("r:push") == "403"
