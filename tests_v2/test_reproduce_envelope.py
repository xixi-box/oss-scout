"""Keep reproduction and Actions envelope regression coverage after scanner retirement."""
from __future__ import annotations
import json
import sys
from scout_v2.adapters import result_envelope as envelope_mod
from scout_v2.schemas import Job
from scout_v2.workers.core import execute, emit_envelope
from scout_v2.workers.reproduce import run_reproduce, sanitized_env

def job(mode="reproduce", **kw):
    values = dict(mode=mode, repo="a/b", base_sha="a" * 40,
                  idempotency_key="verify:a/b:sha:fp", execution_scope="project_contribution",
                  test_cmd=[])
    values.update(kw)
    return Job(**values)


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
        j = job("review")
        r = execute(j, tmp_path / "out")
        emit_envelope(r, tmp_path / "out")
        captured = capsys.readouterr().out
        env = envelope_mod.parse_envelope(captured)
        assert env is not None
        assert env["result"]["job_id"] == j.job_id
        assert env["result"]["outcome"] == "inconclusive"

    def test_parse_garbage_returns_none(self):
        assert envelope_mod.parse_envelope("normal output\nno marker") is None
        assert envelope_mod.parse_envelope(envelope_mod.RESULT_MARKER + "{broken") is None

    def test_parse_actions_log_with_prefix(self):
        # Actions 日志每行带 `step \ttimestamp\t` 前缀
        line = ("smoke\tfake worker run\t2026-10-24T15:41:45Z "
                + envelope_mod.RESULT_MARKER
                + '{"result": {"job_id": "j1", "outcome": "succeeded"}}')
        env = envelope_mod.parse_envelope(line)
        assert env is not None and env["result"]["job_id"] == "j1"
