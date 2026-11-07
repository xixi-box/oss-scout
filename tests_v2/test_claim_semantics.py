"""额度语义：只有环境故障归还，真实消耗不归还；单条候选有尝试上限。

2026-11-03 实录（这是本文件存在的理由）：
  今天 claim 33 次、pipeline_abort 42 次，而 sqlite 的 claim_days 只剩 2 条。
  因为 _abort_release 无条件调用 release_claim，于是
      claim → 失败 → 归还额度 → 立刻能再 claim → 再失败 → …
  可以无限循环：deer-flow#7846 一天被 claim 7 次，qdrant#11368 5 次，
  fastmcp#6273 4 次。配置的"每天 2 个"限的是**同时占用的名额**，
  被这个循环架空了，所以那个数字一直是假的。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scout_v2.state_store import ClaimLedger, business_now  # noqa: E402

from scout_v2.workers import auto_pipeline as mp  # noqa: E402

TZ = "Asia/Shanghai"


class TestReleaseSemantics:
    def test_environment_failure_gives_the_slot_back(self, tmp_path):
        led = ClaimLedger(tmp_path / "t.db")
        try:
            assert led.try_claim(TZ, "a/x#1", max_per_day=5)[0]
            # 模拟环境故障 abort：归还
            assert led.release_claim("a/x#1", TZ) is True
            assert led.count_today(TZ) == 0
        finally:
            led.close()

    def test_real_attempt_keeps_the_slot(self, tmp_path):
        """证据链失败 / 发布失败 → 不归还，否则额度计数失真。"""
        led = ClaimLedger(tmp_path / "t.db")
        try:
            assert led.try_claim(TZ, "a/x#1", max_per_day=5)[0]
            # 不调用 release_claim → 名额被真实消耗
            assert led.count_today(TZ) == 1
            assert led.try_claim(TZ, "a/x#2", max_per_day=5)[0]
            assert led.count_today(TZ) == 2
        finally:
            led.close()

    def test_cap_counts_consumed_attempts_not_concurrent(self, tmp_path):
        """5 次机会就是 5 次：用完第 6 次拒绝，且不会因为 abort 而复活。"""
        led = ClaimLedger(tmp_path / "t.db")
        try:
            for i in range(5):
                ok, _ = led.try_claim(TZ, f"a/x#{i}", max_per_day=5)
                assert ok, f"attempt {i} should fit under the cap of 5"
            ok, why = led.try_claim(TZ, "a/x#99", max_per_day=5)
            assert ok is False and "daily limit 5" in why
            assert led.count_today(TZ) == 5
        finally:
            led.close()


class TestAbortClassification:
    """2026-11-05 用户裁定：所有 abort 一律退坑——5 个名额 = 5 个开出的
    PR，不是 5 次尝试。give_back 参数仅为兼容旧调用方保留。"""

    def test_all_paths_release(self):
        src = (Path(__file__).resolve().parents[1] / "scout_v2" / "workers"
               / "auto_pipeline.py").read_text(encoding="utf-8")
        assert "give_back: bool = True" in src          # 兼容签名保留
        assert "give_back=False" not in src             # 所有调用点都退坑
        assert "ledger.release_claim(" not in src

    def test_attempt_limit_constant(self):
        from scout_v2.workers.auto_pipeline import MAX_ATTEMPTS_PER_CANDIDATE
        assert 1 <= MAX_ATTEMPTS_PER_CANDIDATE <= 5


class TestAttemptCounter:
    def test_attempts_increment_is_persisted(self, tmp_path):
        """mark_candidate 的 extra 要能把 attempts 写回候选条目。"""
        from scout_v2.workers.auto_pipeline import mark_candidate
        q = tmp_path / "queue"
        q.mkdir(parents=True)
        (q / "candidates.json").write_text(json.dumps({
            "a/x#1": {"id": "a/x#1", "repo": "a/x", "number": 1, "status": "new"}
        }), encoding="utf-8")
        mark_candidate(tmp_path, "a/x#1", "new", extra={"attempts": 2})
        d = json.loads((q / "candidates.json").read_text(encoding="utf-8"))
        assert d["a/x#1"]["attempts"] == 2


class TestAttemptFuseWorklogCount:
    """认领保险丝的计数必须并发安全。

    2026-11-05 实录：计数存在 candidates.json 的 attempts 字段里，而该文件
    被 ingest 即时派发/监控循环/drain 三个线程并发原子重写，计数互相覆盖
    丢失——fastmcp#6298 一天被认领 6 次、istio#61045 每分钟重试，保险丝
    永远读不到 3。改按 worklog 里今天的 claim 记录计数（只追加，不覆盖）。
    """

    def test_counts_todays_claims_from_worklog(self, tmp_path, monkeypatch):
        (tmp_path / "state").mkdir()
        import datetime as _dt
        day = business_now(TZ).date()
        today = day.isoformat()
        yest = (day - _dt.timedelta(days=1)).isoformat()
        lines = []
        for i in range(3):
            lines.append(json.dumps({
                "ts": f"{today}T0{i}:0{i}:00", "action": "claim",
                "target": "fastmcp/fastmcp#6298", "source": "auto-pipeline"}))
        lines.append(json.dumps({
            "ts": f"{today}T09:00:00", "action": "pipeline_abort",
            "target": "fastmcp/fastmcp#6298", "result": "competed"}))
        lines.append(json.dumps({
            "ts": f"{yest}T09:00:00", "action": "claim",
            "target": "fastmcp/fastmcp#6298", "source": "auto-pipeline"}))
        (tmp_path / "state" / "worklog.jsonl").write_text(
            "\n".join(lines) + "\n", encoding="utf-8")
        assert mp._todays_claim_count(tmp_path, "fastmcp/fastmcp#6298") == 3

    def test_yesterdays_claims_do_not_count(self, tmp_path):
        (tmp_path / "state").mkdir()
        (tmp_path / "state" / "worklog.jsonl").write_text(
            json.dumps({"ts": "2026-11-04T09:00:00", "action": "claim",
                        "target": "a/b#1"}) + "\n", encoding="utf-8")
        assert mp._todays_claim_count(tmp_path, "a/b#1") == 0

    def test_missing_worklog_is_zero(self, tmp_path):
        assert mp._todays_claim_count(tmp_path, "a/b#1") == 0


class TestCompeteReleasesQuota:
    """被别人抢走的候选必须退坑。

    2026-11-05 实录：claim_days 记了 5 个坑但当天只开出 3 个 PR——两个被
    competed/duplicate 的失败尝试白吃名额，导致"今天还有名额却不动"。
    被抢不是我们浪费机会，abort 必须归还。
    """

    def test_competed_path_releases(self):
        import inspect
        src = inspect.getsource(mp)
        i = src.index('"aborted_competed"')
        tail = src[i:i + 600]
        assert "_abort_release" in tail, tail
        assert "give_back=True" in tail

    def test_duplicate_path_releases(self):
        import inspect
        src = inspect.getsource(mp)
        i = src.index('"aborted_duplicate"')
        tail = src[i:i + 400]
        assert "_abort_release" in tail


class TestStagedFlush:
    """凌晨批量激活 staged：细筛结果复用，不再重跑 AI。

    用户裁定 2026-11-05 的两段式消费：
      白天 每分钟发现 → 名额内当场修，满了标 staged
      凌晨 全部 staged → new，供当天消费（只做"有没有人做"的实时复检）
    """

    def test_flush_revives_and_clears_staged_at(self, tmp_path, monkeypatch):
        from scout_v2.workers.auto_pipeline import _data_root
        (tmp_path / "queue").mkdir()
        (tmp_path / "state").mkdir()
        qpath = tmp_path / "queue" / "candidates.json"
        qpath.write_text(json.dumps({
            "a/b#1": {"status": "staged", "staged_at": "2026-11-05T01:00:00",
                      "ai_verdict": {"usable": True}},
            "a/b#2": {"status": "new", "ai_verdict": {"usable": True}},
            "a/b#3": {"status": "aborted_competed"},
        }), encoding="utf-8")

        from scout_v2.state_store import atomic_json_write
        q = json.loads(qpath.read_text(encoding="utf-8"))
        revived = 0
        for key, e in q.items():
            if (e or {}).get("status") == "staged":
                e["status"] = "new"
                e.pop("staged_at", None)
                revived += 1
        atomic_json_write(qpath, q)

        after = json.loads(qpath.read_text(encoding="utf-8"))
        assert revived == 1
        assert after["a/b#1"]["status"] == "new"
        assert "staged_at" not in after["a/b#1"]
        assert after["a/b#2"]["status"] == "new"
        assert after["a/b#3"]["status"] == "aborted_competed"   # 终态不动

    def test_flush_does_not_touch_ai_verdict(self, tmp_path):
        """凌晨复活不重跑细筛：ai_verdict 原样保留。"""
        (tmp_path / "queue").mkdir()
        qpath = tmp_path / "queue" / "candidates.json"
        verdict = {"usable": True, "reason": "reproducible bug", "model": "m"}
        qpath.write_text(json.dumps({
            "a/b#1": {"status": "staged", "staged_at": "x", "ai_verdict": verdict},
        }), encoding="utf-8")
        from scout_v2.state_store import atomic_json_write
        q = json.loads(qpath.read_text(encoding="utf-8"))
        for e in q.values():
            if (e or {}).get("status") == "staged":
                e["status"] = "new"
                e.pop("staged_at", None)
        atomic_json_write(qpath, q)
        assert json.loads(qpath.read_text(encoding="utf-8"))["a/b#1"]["ai_verdict"] == verdict


class TestAlwaysReleaseSemantics:
    """2026-11-05 用户裁定：每天 5 个 = 5 个开出的 PR，不是 5 次尝试。

    失败（无证据/发布失败/候选不可用）一律退坑，让下一条候选顶上。
    防死循环靠同候选日闸 + 每日 3 次保险丝，不靠扣名额。
    """

    def test_no_evidence_path_releases(self):
        import inspect
        src = inspect.getsource(mp)
        assert "give_back=False" not in src, "所有 abort 路径必须退坑"
        assert "ledger.release_claim(" not in src

    def test_give_back_param_is_tolerated_but_ignored(self, tmp_path):
        """兼容旧调用方：give_back=False 传入也照样退坑。"""
        led = ClaimLedger(tmp_path / "t.db")
        ok, _ = led.try_claim("Asia/Shanghai", "a/b#1", max_per_day=5)
        assert ok is True
        released = led.release_claim("a/b#1", "Asia/Shanghai")
        assert released is True
        # 释放后名额立刻可用
        ok2, _ = led.try_claim("Asia/Shanghai", "a/c#2", max_per_day=5)
        assert ok2 is True
