"""P4 测试：OpenCode CLI 客户端（事件流解析、真实 401 形状）+ 动态模型发现 + 回退链集成。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scout_v2.workers.model_fallback import (AuthError, ModelPolicy,  # noqa: E402
                                             PaidDisabledError, run_with_fallback)
from scout_v2.workers.opencode_client import (OpenCodeClient,  # noqa: E402
                                              discover_free_models)

# 实测（2026-10-25）opencode-go key 过期时的真实错误事件形状
REAL_401_EVENT = json.dumps({
    "type": "error",
    "timestamp": 1792857932966,
    "sessionID": "ses_x",
    "error": {"name": "APIError", "data": {
        "message": "This API key has expired.",
        "statusCode": 401, "isRetryable": False,
        "responseBody": '{"error":{"type":"authentication_error","code":"api_key_expired"}}',
        "metadata": {"url": "https://opencode.ai/zen/go/v1/chat/completions"}}}})


class TestOpenCodeClient:
    def _client_with_stdout(self, stdout, rc=0, stderr=""):
        import scout_v2.workers.opencode_client as oc

        def fake_run(cmd, **kw):
            class P:
                pass
            p = P()
            p.stdout, p.stderr, p.returncode = stdout, stderr, rc
            return p
        c = OpenCodeClient()
        monkey = pytest.MonkeyPatch()
        monkey.setattr(oc.subprocess, "run", fake_run)
        return c, monkey

    def test_success_text_events(self):
        events = "\n".join([
            json.dumps({"type": "session", "id": "s1"}),
            json.dumps({"type": "text", "text": "OK"}),
        ])
        c, monkey = self._client_with_stdout(events)
        ok, out = c.run("m", "p")
        monkey.undo()
        assert ok and out == "OK"

    def test_real_401_event_classified(self):
        c, monkey = self._client_with_stdout(REAL_401_EVENT, rc=1)
        ok, detail = c.run("opencode-go/deepseek-v4-flash", "p")
        monkey.undo()
        assert not ok and "401" in detail and "expired" in detail

    def test_401_stops_chain_via_fallback(self):
        """真实 401 → AuthError 停链 + 报警，绝不换挡/付费。"""
        alerts = []
        c, monkey = self._client_with_stdout(REAL_401_EVENT, rc=1)
        policy = ModelPolicy(primary_models=["opencode-go/deepseek-v4-flash"],
                             free_preferences=["other/model"])
        with pytest.raises(AuthError):
            run_with_fallback(policy, c, "p", on_alert=alerts.append)
        monkey.undo()
        assert alerts

    def test_rate_limit_falls_through(self):
        ev = json.dumps({"type": "error", "error": {"name": "APIError", "data": {
            "message": "rate limited", "statusCode": 429}}})
        c, monkey = self._client_with_stdout(ev, rc=1)
        policy = ModelPolicy(primary_models=["a/1"], free_preferences=["b/2"])
        calls = {"n": 0}

        class Next:
            def run(self, model, prompt):
                calls["n"] += 1
                if model == "b/2":
                    return True, "done"
                # 委托给被 monkeypatch 的 c（返回 429）
                return c.run(model, prompt)

        model, out = run_with_fallback(policy, Next(), "p")
        monkey.undo()
        assert model == "b/2"

    def test_binary_missing(self, monkeypatch):
        import scout_v2.workers.opencode_client as oc
        def raise_fnf(cmd, **kw):
            raise FileNotFoundError()
        monkeypatch.setattr(oc.subprocess, "run", raise_fnf)
        ok, detail = OpenCodeClient().run("m", "p")
        assert not ok and "not found" in detail

    def test_empty_response(self):
        c, monkey = self._client_with_stdout("", rc=0)
        ok, detail = c.run("m", "p")
        monkey.undo()
        assert not ok and "empty" in detail


class TestDiscoverFreeModels:
    def test_filters_free_suffix(self, monkeypatch):
        import scout_v2.workers.opencode_client as oc
        monkeypatch.setattr(oc.subprocess, "run", lambda *a, **kw: type(
            "P", (), {"stdout": "opencode/a-free\nopencode/paid\n opencode/b-free \n",
                      "returncode": 0})())
        assert discover_free_models() == ["opencode/a-free", "opencode/b-free"]

    def test_cli_missing_empty(self, monkeypatch):
        import scout_v2.workers.opencode_client as oc
        def raise_fnf(*a, **kw):
            raise FileNotFoundError()
        monkeypatch.setattr(oc.subprocess, "run", raise_fnf)
        assert discover_free_models() == []


class TestPolicyConfig:
    def test_yaml_loads_fixed_mode(self):
        import yaml
        root = Path(__file__).resolve().parents[1]
        cfg = yaml.safe_load((root / "configs" / "model_policy.yaml").read_text(encoding="utf-8"))
        p = ModelPolicy.from_yaml(cfg)
        assert p.mode == "fixed"
        assert p.models()[0] == "opencode-go/deepseek-v4-flash"
        assert p.allow_paid_api is True          # 用户批准的订阅通道
        assert p.paid_fallback_enabled is False  # 无第二付费兜底
