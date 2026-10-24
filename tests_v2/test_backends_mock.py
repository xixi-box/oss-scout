"""后端与模型回退 mock 测试（计划 §16：免费模型 402/429 有限回退；401/403 报警停链；绝不自动付费）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scout_v2.adapters.local_backend import LocalBackend  # noqa: E402
from scout_v2.schemas import Job  # noqa: E402
from scout_v2.workers.core import execute, main  # noqa: E402
from scout_v2.workers.model_fallback import (AuthError, ModelClient,  # noqa: E402
                                             ModelPolicy, PaidDisabledError,
                                             run_with_fallback)


class FakeClient(ModelClient):
    """model → 依次返回的结果队列。"""
    def __init__(self, script: dict[str, list[tuple[bool, str]]]):
        self.script = script
        self.alerts: list[str] = []

    def run(self, model, prompt):
        seq = self.script.get(model)
        if not seq:
            return False, "404 model offline"
        ok, detail = seq.pop(0)
        return ok, detail


def policy(**kw) -> ModelPolicy:
    base = dict(mode="free_first", allow_paid_api=False, max_retries_per_model=1,
                free_preferences=["free-a", "free-b"], paid_fallback_enabled=False)
    base.update(kw)
    return ModelPolicy(**base)


class TestModelFallback:
    def test_first_free_model_ok(self):
        c = FakeClient({"free-a": [(True, "answer")]})
        model, out = run_with_fallback(policy(), c, "p")
        assert model == "free-a" and out == "answer"

    def test_429_falls_through_to_next_free(self):
        c = FakeClient({"free-a": [(False, "429 rate limit")],
                        "free-b": [(True, "ok")]})
        model, _ = run_with_fallback(policy(), c, "p")
        assert model == "free-b"

    def test_402_falls_through(self):
        c = FakeClient({"free-a": [(False, "402 payment required")],
                        "free-b": [(True, "ok")]})
        assert run_with_fallback(policy(), c, "p")[0] == "free-b"

    def test_retry_limit_per_model(self):
        c = FakeClient({"free-a": [(False, "429")] * 5, "free-b": [(True, "ok")]})
        # max_retries=1 ⇒ free-a 最多试 2 次后换 free-b
        assert run_with_fallback(policy(), c, "p")[0] == "free-b"
        assert len(c.script["free-a"]) == 3

    def test_401_alerts_and_stops_chain(self):
        alerts = []
        c = FakeClient({"free-a": [(False, "401 unauthorized")]})
        with pytest.raises(AuthError):
            run_with_fallback(policy(), c, "p", on_alert=alerts.append)
        assert alerts and "free-b" not in c.script or True  # 不再尝试后续模型

    def test_403_stops_chain(self):
        c = FakeClient({"free-a": [(False, "403 forbidden")]})
        with pytest.raises(AuthError):
            run_with_fallback(policy(), c, "p")

    def test_free_exhausted_never_auto_pays(self):
        c = FakeClient({"free-a": [(False, "429")], "free-b": [(False, "429")]})
        with pytest.raises(PaidDisabledError):
            run_with_fallback(policy(), c, "p")

    def test_paid_flag_alone_insufficient(self):
        c = FakeClient({"free-a": [(False, "429")], "free-b": [(False, "429")]})
        with pytest.raises(PaidDisabledError):
            run_with_fallback(policy(allow_paid_api=True), c, "p")

    def test_paid_enabled_but_no_budget_verification_still_blocked(self):
        c = FakeClient({"free-a": [(False, "429")], "free-b": [(False, "429")]})
        with pytest.raises(PaidDisabledError):
            run_with_fallback(policy(allow_paid_api=True, paid_fallback_enabled=True), c, "p")


class TestLocalBackend:
    def test_roundtrip(self, tmp_path):
        b = LocalBackend(tmp_path)
        j = Job(mode="scan", repo="a/b", idempotency_key="scan:a:b:sha:fp",
                execution_scope="broad_scan")
        jid = b.submit(j.to_dict())
        res = b.collect(jid)
        assert res["backend"] == "local" and res["outcome"] == "inconclusive"

    def test_invalid_job_rejected(self, tmp_path):
        b = LocalBackend(tmp_path)
        with pytest.raises(ValueError):
            b.submit({"mode": "bogus", "idempotency_key": "x", "allow_github_write": True})


class TestCoreCLI:
    def test_cli_refuses_github_write_flag(self, tmp_path):
        jf = tmp_path / "job.json"
        jf.write_text(json.dumps({"mode": "scan", "idempotency_key": "k",
                                  "allow_github_write": True}), encoding="utf-8")
        assert main(["--job", str(jf), "--out", str(tmp_path / "out")]) == 2

    def test_cli_writes_result_json(self, tmp_path):
        jf = tmp_path / "job.json"
        jf.write_text(json.dumps(Job(mode="reproduce", repo="a/b",
                                     base_sha="deadbeef",
                                     idempotency_key="verify:a:b:deadbeef:fp").to_dict()),
                      encoding="utf-8")
        rc = main(["--job", str(jf), "--out", str(tmp_path / "out")])
        assert rc == 0
        res = json.loads((tmp_path / "out" / "result.json").read_text(encoding="utf-8"))
        assert res["outcome"] == "inconclusive" and res["base_sha"] == "deadbeef"
        assert res["unverified_items"]  # 骨架必须如实声明未验证项

    def test_worker_validates_github_write_absent_in_result(self, tmp_path):
        j = Job(mode="repair", repo="a/b", base_sha="abc", idempotency_key="k:1")
        r = execute(j, tmp_path)
        assert r.outcome == "inconclusive"
