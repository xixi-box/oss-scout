"""Broker transport failures must not turn a live repair into a code failure."""
import io
import json
import subprocess
import urllib.error
from pathlib import Path

import pytest

from scout_v2.workers import codex_client as module
from scout_v2.workers.codex_client import (
    CODEX_COMMUNICATION_RETRY_PREFIX, CodexClient, CodexCommunicationError,
)


@pytest.mark.parametrize("status,retryable", [(503, True), (502, True), (429, True), (409, False)])
def test_temporary_http_errors_have_safe_typed_details(tmp_path, monkeypatch, status, retryable):
    monkeypatch.setenv("DASHBOARD_URL", "https://controller.example")
    monkeypatch.setenv("CF_INGEST_TOKEN", "private-ingest-token")
    def request(*args, **kwargs):
        raise urllib.error.HTTPError("https://controller.example", status,
                                     "private-token-in-message", {}, io.BytesIO(b"private-body"))
    monkeypatch.setattr(module.urllib.request, "urlopen", request)
    with pytest.raises(CodexCommunicationError) as error:
        CodexClient(timeout_seconds=2400, workdir=tmp_path)._exchange("sync", lease="private-lease")
    assert error.value.retryable is retryable
    assert str(error.value).startswith(CODEX_COMMUNICATION_RETRY_PREFIX)
    assert f"HTTP {status}" in str(error.value)
    assert "private" not in str(error.value)


def test_broker_timeout_reports_operation_without_leaking_exception_message(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_URL", "https://controller.example")
    monkeypatch.setenv("CF_INGEST_TOKEN", "private-ingest-token")
    def request(*args, **kwargs):
        assert kwargs['timeout'] == 3
        raise TimeoutError("private-token")
    monkeypatch.setattr(module.urllib.request, "urlopen", request)
    with pytest.raises(CodexCommunicationError) as error:
        CodexClient(timeout_seconds=2400, workdir=tmp_path)._exchange("sync", request_timeout=3)
    assert "operation=sync" in str(error.value) and "TimeoutError" in str(error.value)
    assert "private" not in str(error.value)


def heartbeat_client(tmp_path, monkeypatch, failures, *, complete_during_timeout=False):
    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module.time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    monkeypatch.setattr(module, "_prepare_process_cleanup", lambda: (set(), False))
    monkeypatch.setattr(module, "_restore_subreaper", lambda _: None)
    states = []
    monkeypatch.setattr(module, "_stop_process_tree", lambda process, baseline:
                        states.append(bool(process and process.done)) or [])
    process_ref = []
    class Process:
        def __init__(self, args, **kwargs):
            self.output = Path(args[args.index("-o") + 1])
            self.done = False
            self.waits = 0
            process_ref.append(self)
        def finish(self):
            self.output.write_text("fixed with regression", encoding="utf-8")
            self.done = True
        def wait(self, timeout):
            self.waits += 1
            if self.done:
                return 0
            if self.waits == 1:
                clock[0] += timeout
                raise subprocess.TimeoutExpired("codex", timeout)
            self.finish()
            return 0
        def poll(self):
            return 0 if self.done else None
    monkeypatch.setattr(module.subprocess, "Popen", Process)
    client = CodexClient(timeout_seconds=2400, workdir=tmp_path, diagnostics_dir=tmp_path / "artifacts")
    calls = []
    def exchange(operation, **values):
        calls.append(operation)
        if operation == "acquire":
            return {"lease":"private-lease", "auth":{"tokens":{
                "access_token":"private-access", "refresh_token":"private-refresh"}}}
        if operation == "sync" and calls.count("sync") <= failures:
            clock[0] += values.get("request_timeout", 15)
            if complete_during_timeout:
                process_ref[0].finish()
            raise TimeoutError("private-access")
        return {"ok": True}
    monkeypatch.setattr(client, "_exchange", exchange)
    return client, calls, clock, states


def test_one_heartbeat_timeout_recovers_without_stopping_the_cli(tmp_path, monkeypatch):
    client, calls, clock, cleanup = heartbeat_client(tmp_path, monkeypatch, 1)
    ok, detail = client.run("test#medium", "fix")
    assert ok and detail == "fixed with regression"
    assert calls == ["acquire", "sync", "sync", "release"]
    assert cleanup == [True]
    rows = [json.loads(line) for line in (tmp_path / "artifacts/codex-events.jsonl").read_text().splitlines()]
    assert next(row for row in rows if row['type']=='auth.heartbeat_retry')['request_seconds'] == 15
    assert next(row for row in rows if row['type']=='auth.heartbeat_ok')['attempt'] == 2
    assert "private" not in (tmp_path / "artifacts/codex-events.jsonl").read_text()


def test_continuous_heartbeat_timeouts_stop_before_the_lease_expires(tmp_path, monkeypatch):
    client, calls, clock, cleanup = heartbeat_client(tmp_path, monkeypatch, 999)
    ok, detail = client.run("test#medium", "fix")
    assert not ok and detail.startswith(CODEX_COMMUNICATION_RETRY_PREFIX)
    assert "lease renewal deadline" in detail
    assert calls.count("sync") > 1 and calls[-1] == "release"
    assert clock[0] <= 115  # 120-second server lease with a five-second safety margin.
    assert cleanup == [False]


def test_cli_completed_during_a_lost_heartbeat_can_keep_its_result(tmp_path, monkeypatch):
    client, calls, clock, cleanup = heartbeat_client(tmp_path, monkeypatch, 1, complete_during_timeout=True)
    ok, detail = client.run("test", "fix")
    assert ok and detail == "fixed with regression"
    assert calls.count("sync") == 1 and cleanup == [True]
