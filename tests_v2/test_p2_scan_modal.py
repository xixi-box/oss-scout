"""P2 测试：ModalCliBilling 防御解析、scan/reproduce 真实逻辑、Modal 后端闸门、stdout 信封。"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scout_v2.adapters import modal_backend as mb_mod  # noqa: E402
from scout_v2.budget import BudgetGuard, ModalCliBilling  # noqa: E402
from scout_v2.schemas import Job  # noqa: E402
from scout_v2.workers.core import execute, emit_envelope  # noqa: E402
from scout_v2.workers.reproduce import run_reproduce, sanitized_env  # noqa: E402
from scout_v2.workers.scan import (bug_candidate_id, bug_fingerprint,  # noqa: E402
                                   run_scan)


def job(mode="scan", **kw) -> Job:
    base = dict(mode=mode, repo="a/b", base_sha="a" * 40,
                idempotency_key=f"scan:a/b:{'a' * 40}:fp",
                execution_scope="broad_scan", test_cmd=[])
    base.update(kw)
    return Job(**base)


class TestModalCliBilling:
    def _billing(self, rc=0, out=""):
        return ModalCliBilling(runner=lambda cmd: (rc, out))

    def test_nested_cost_extracted(self):
        b = self._billing(out=json.dumps({"period": {"total_cost_usd": 1.23}}))
        assert b.healthy() and b.metered_usage_usd() == pytest.approx(1.23)

    def test_flat_cost_extracted(self):
        b = self._billing(out=json.dumps({"metered_cost": 0.75, "other": "x"}))
        assert b.metered_usage_usd() == pytest.approx(0.75)

    def test_unknown_structure_fails_closed(self):
        b = self._billing(out=json.dumps({"foo": {"bar": 1}}))
        assert b.metered_usage_usd() is None

    def test_cli_failure_unhealthy(self):
        b = self._billing(rc=1, out="")
        assert not b.healthy()

    def test_garbage_output_fails_closed(self):
        b = self._billing(out="not json at all")
        assert b.metered_usage_usd() is None

    def test_decimal_string_form_accepted(self):
        # 实测 2026-10-25：modal 返回 Decimal 字符串（"0E-8"）
        b = self._billing(out=json.dumps({"metered_cost": "0E-8"}))
        assert b.metered_usage_usd() == 0.0

    def test_dollar_prefixed_string_accepted(self):
        b = self._billing(out=json.dumps({"total_cost": "$12.34"}))
        assert b.metered_usage_usd() == pytest.approx(12.34)

    def test_bool_not_treated_as_cost(self):
        b = self._billing(out=json.dumps({"cost": True}))
        assert b.metered_usage_usd() is None


def guard(metered=0.0, enabled=True) -> BudgetGuard:
    class B:
        def healthy(self):
            return True
        def metered_usage_usd(self):
            return metered
    return BudgetGuard(soft_cap_usd=25.0, enabled=enabled, billing=B())


class TestModalBackendGates:
    def test_disabled_flag_refuses(self):
        b = mb_mod.ModalSandboxBackend(guard(enabled=False))
        with pytest.raises(mb_mod.BackendDisabledError):
            b.submit(job().to_dict())

    def test_budget_refuses(self):
        b = mb_mod.ModalSandboxBackend(guard(metered=24.9))
        with pytest.raises(mb_mod.BudgetExceededError):
            b.submit(job(max_estimated_compute_usd=1.0).to_dict())

    def test_missing_sdk_refuses_after_budget_ok(self, monkeypatch):
        # 预算通过但 SDK 不可用 → 干净拒绝（显式模拟 modal 缺失，不依赖本机是否安装）
        import sys
        monkeypatch.setitem(sys.modules, "modal", None)  # import modal → ImportError
        b = mb_mod.ModalSandboxBackend(guard(metered=0.0))
        with pytest.raises(mb_mod.BackendDisabledError) as ei:
            b.submit(job(max_estimated_compute_usd=0.5).to_dict())
        assert "SDK" in str(ei.value)

    def test_eligible_mirrors_budget(self):
        b = mb_mod.ModalSandboxBackend(guard(metered=24.9))
        assert not b.eligible({"max_estimated_compute_usd": 1.0})


class TestScan:
    SEMGREP_OUT = json.dumps({
        "results": [
            {"path": "src/mod.py", "check_id": "py.sec.injection",
             "start": {"line": 42}, "extra": {"message": "possible injection"}},
            {"path": "src/other.py", "check_id": "py.bug.offbyone",
             "start": {"line": 7}, "extra": {"message": "off-by-one"}},
        ]})

    def test_findings_with_stable_fingerprints(self, tmp_path):
        j = job()
        r = run_scan(j, tmp_path / "repo", tmp_path / "out",
                     runner=lambda cmd: (1, self.SEMGREP_OUT, ""))
        assert r.outcome == "inconclusive"           # 线索≠证据
        data = json.loads((tmp_path / "out" / "findings.json").read_text(encoding="utf-8"))
        f0 = data["findings"][0]
        assert f0["candidate_id"] == bug_candidate_id(
            "a/b", j.base_sha, bug_fingerprint("a/b", "src/mod.py", "py.sec.injection", 42))
        assert f0["fingerprint"] == bug_fingerprint(
            "a/b", "src/mod.py", "py.sec.injection", 42)

    def test_fingerprint_depends_on_location(self):
        assert (bug_fingerprint("a/b", "p.py", "r", 1)
                != bug_fingerprint("a/b", "p.py", "r", 2))

    def test_semgrep_missing_inconclusive(self, tmp_path):
        def raise_fnf(cmd):
            raise FileNotFoundError("semgrep")
        r = run_scan(job(), tmp_path, tmp_path / "o", runner=raise_fnf)
        assert r.outcome == "inconclusive" and any("semgrep" in u for u in r.unverified_items)

    def test_semgrep_error_failed(self, tmp_path):
        r = run_scan(job(), tmp_path, tmp_path / "o",
                     runner=lambda cmd: (2, "", "ruleset not found"))
        assert r.outcome == "failed"

    def test_unparseable_output_failed(self, tmp_path):
        r = run_scan(job(), tmp_path, tmp_path / "o", runner=lambda cmd: (0, "{broken", ""))
        assert r.outcome == "failed" and any("unparseable" in u for u in r.unverified_items)

    def test_max_findings_cap(self, tmp_path):
        many = {"results": [{"path": "p.py", "check_id": f"r{i}", "start": {"line": i},
                             "extra": {"message": "m"}} for i in range(100)]}
        r = run_scan(job(), tmp_path, tmp_path / "o",
                     runner=lambda cmd: (1, json.dumps(many), ""), max_findings=10)
        data = json.loads((tmp_path / "o" / "findings.json").read_text(encoding="utf-8"))
        assert len(data["findings"]) == 10


class TestReproduce:
    def test_base_failure_counts_as_reproduced(self, tmp_path):
        j = job("reproduce", test_cmd=["python", "-c", "raise SystemExit(1)"],
                idempotency_key="verify:a/b:" + "a" * 40 + ":fp")
        r = run_reproduce(j, tmp_path, tmp_path / "o",
                          test_cmd=list(j.test_cmd),
                          runner=lambda cmd, cwd, env: (1, "", "assert failed"))
        assert r.reproduced_on_base is True
        assert not r.fixed_on_patch            # 不宣称修复
        rep = json.loads((tmp_path / "o" / "test-report.json").read_text(encoding="utf-8"))
        assert rep["returncode"] == 1

    def test_base_pass_means_not_reproduced(self, tmp_path):
        j = job("reproduce", test_cmd=["python", "-c", "pass"])
        r = run_reproduce(j, tmp_path, tmp_path / "o", test_cmd=list(j.test_cmd),
                          runner=lambda cmd, cwd, env: (0, "ok", ""))
        assert r.reproduced_on_base is False

    def test_command_not_runnable_is_not_evidence(self, tmp_path):
        r = run_reproduce(job("reproduce"), tmp_path, tmp_path / "o",
                          test_cmd=["nope"],
                          runner=lambda cmd, cwd, env: (127, "", "not found"))
        assert r.reproduced_on_base is False
        assert any("not runnable" in u for u in r.unverified_items)

    def test_no_test_cmd_blocked(self, tmp_path):
        r = run_reproduce(job("reproduce"), tmp_path, tmp_path / "o", test_cmd=[])
        assert r.outcome == "blocked"

    def test_env_sanitized(self):
        base = {"PATH": "/usr/bin", "MY_API_KEY": "sk-123", "GH_TOKEN": "ghp_x",
                "MODAL_TOKEN_SECRET": "s", "HOME": "/h", "SYSTEMROOT": "C:\\Windows"}
        env = sanitized_env(base, home="/tmp/sb")
        assert env["PATH"] == "/usr/bin"           # 白名单保留
        assert env["SYSTEMROOT"] == "C:\\Windows"
        assert "MY_API_KEY" not in env
        assert "GH_TOKEN" not in env
        assert "MODAL_TOKEN_SECRET" not in env
        assert env["HOME"] == "/tmp/sb"
        # 报告只记键名不记值
        assert all("sk-123" not in k for k in env)

    def test_real_subprocess_env_isolation(self, tmp_path):
        """端到端：子进程实际拿不到注入的 token。"""
        j = job("reproduce", test_cmd=["python", "-c",
              "import os,sys;sys.exit(0 if 'GH_TOKEN' not in os.environ else 1)"])
        r = run_reproduce(j, tmp_path, tmp_path / "o", test_cmd=list(j.test_cmd),
                          env_base={"PATH": "", "GH_TOKEN": "leak-me", "SYSTEMROOT": "x"})
        assert r.reproduced_on_base is False       # 测试通过（没拿到 token）⇒ base 不算复现


class TestEnvelope:
    def test_emit_and_parse_roundtrip(self, tmp_path, capsys):
        j = job("scan")
        r = execute(j, tmp_path / "out")           # local 无 semgrep → inconclusive，但可发信封
        emit_envelope(r, tmp_path / "out")
        captured = capsys.readouterr().out
        env = mb_mod.parse_envelope(captured)
        assert env is not None
        assert env["result"]["job_id"] == j.job_id
        assert env["result"]["outcome"] == "inconclusive"

    def test_parse_garbage_returns_none(self):
        assert mb_mod.parse_envelope("normal output\nno marker") is None
        assert mb_mod.parse_envelope(mb_mod.RESULT_MARKER + "{broken") is None

    def test_parse_actions_log_with_prefix(self):
        # Actions 日志每行带 `step \ttimestamp\t` 前缀
        line = ("smoke\tfake worker run\t2026-10-24T15:41:45Z "
                + mb_mod.RESULT_MARKER
                + '{"result": {"job_id": "j1", "outcome": "succeeded"}}')
        env = mb_mod.parse_envelope(line)
        assert env is not None and env["result"]["job_id"] == "j1"
