import io
import json
from threading import Lock

import pytest

from scout_v2 import codex_auth as auth


def github_mode(root):
    (root / "state").mkdir(exist_ok=True)
    (root / "state/codex_auth_transport.json").write_text('{"mode":"github_secrets"}')


def github_response(monkeypatch, state, calls):
    monkeypatch.setenv("GH_TOKEN", "private-github-token")
    def request(req, **kwargs):
        calls.append(req.full_url)
        assert req.full_url == "https://api.github.com/repos/xixi-box/oss-scout/environments/codex-auth/variables/CODEX_AUTH_STATE"
        assert kwargs["timeout"] == 8
        return io.BytesIO(json.dumps({"value": json.dumps({"state": state})}).encode())
    monkeypatch.setattr(auth.urllib.request, "urlopen", request)


@pytest.mark.parametrize("state,ready", [("ready", True), ("in_use", False), ("blocked", False), ("unknown", False)])
def test_github_guard_is_the_only_readiness_source(tmp_path, monkeypatch, state, ready):
    auth.install_auth(tmp_path, {"tokens": {"access_token": "old", "refresh_token": "old"}})
    github_mode(tmp_path)
    calls = []
    github_response(monkeypatch, state, calls)
    assert auth.auth_ready(tmp_path) is ready
    assert auth.auth_ready(tmp_path) is ready
    assert len(calls) == 1
    for operation in ("acquire", "sync", "release", "refresh", "refresh_begin", "invalid"):
        assert auth.exchange(tmp_path, {"operation": operation})[1] == 410
    with pytest.raises(RuntimeError, match="disabled"):
        auth.install_auth(tmp_path, {"tokens": {"access_token": "new", "refresh_token": "new"}})
    assert json.loads(auth.auth_path(tmp_path).read_text())["tokens"]["refresh_token"] == "old"


def test_expired_ready_cache_does_not_hide_a_transport_failure(tmp_path, monkeypatch, capsys):
    github_mode(tmp_path)
    clock = [0]
    monkeypatch.setattr(auth.time, "monotonic", lambda: clock[0])
    github_response(monkeypatch, "ready", [])
    assert auth.auth_ready(tmp_path)
    clock[0] = 6
    def timeout(*args, **kwargs):
        raise TimeoutError("private-token-response-body")
    monkeypatch.setattr(auth.urllib.request, "urlopen", timeout)
    assert not auth.auth_ready(tmp_path)
    assert auth.github_auth_state(tmp_path) == "unavailable"
    assert "private" not in capsys.readouterr().out


@pytest.mark.parametrize("marker", ["not json", "[]", '{"mode":"invalid"}'])
def test_invalid_transport_marker_never_falls_back_to_server_credentials(tmp_path, marker):
    auth.install_auth(tmp_path, {"tokens": {"access_token": "old", "refresh_token": "old"}})
    github_mode(tmp_path)
    (tmp_path / "state/codex_auth_transport.json").write_text(marker)
    assert not auth.auth_ready(tmp_path)
    assert auth.exchange(tmp_path, {"operation": "acquire"})[1] == 410


def test_missing_github_token_does_not_use_server_credentials(tmp_path, monkeypatch):
    github_mode(tmp_path)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    assert not auth.auth_ready(tmp_path)


@pytest.mark.parametrize("state,expected", [("ready", "已配置"), ("in_use", "使用中"), ("blocked", "暂不可用")])
def test_feishu_transport_status_does_not_mislabel_a_login_problem(tmp_path, monkeypatch, state, expected):
    from scout_v2 import feishu_models as fm
    github_mode(tmp_path)
    github_response(monkeypatch, state, [])
    monkeypatch.setattr(fm.fb, "configured", lambda _: True)
    monkeypatch.setattr(fm.fb, "_binding_app_token", lambda _: "base")
    monkeypatch.setattr(fm.fb, "load_binding", lambda _: {"app_token": "base", "model_table_id": "table",
        "model_table_layout": 6, "model_table_name": fm.TABLE})
    monkeypatch.setattr(fm.fb, "load_records", lambda *args: {})
    created = []
    monkeypatch.setattr(fm, "_table_request", lambda url, method, path, payload:
                        created.extend(payload["records"]) or {})
    fm.sync_model_settings(tmp_path, Lock())
    fields = next(row["fields"] for row in created if row["fields"]["键"] == "Codex")
    assert "GitHub Secrets " + expected in fields["应用状态"]
    assert "登录" not in fields["应用状态"]


@pytest.mark.parametrize("state,allowed", [("ready", True), ("in_use", True), ("blocked", False)])
def test_switching_to_codex_allows_a_busy_configured_session(tmp_path, monkeypatch, state, allowed):
    from scout_v2 import feishu_models as fm
    from scout_v2.console_models import effective_models
    github_mode(tmp_path)
    github_response(monkeypatch, state, [])
    models = effective_models(tmp_path)
    records = {"运行设置": {"record_id": "runtime", "fields": {
        "修复工具": "Codex", "候选池大小": int(models["candidate_pool_size"]),
        "每日 PR 额度": int(models["daily_pr_quota"])}}}
    monkeypatch.setattr(fm.fb, "configured", lambda _: True)
    monkeypatch.setattr(fm.fb, "_binding_app_token", lambda _: "base")
    monkeypatch.setattr(fm.fb, "load_binding", lambda _: {"app_token": "base", "model_table_id": "table",
        "model_table_layout": 6, "model_table_name": fm.TABLE})
    monkeypatch.setattr(fm.fb, "load_records", lambda *args: records)
    updated = []
    monkeypatch.setattr(fm, "_table_request", lambda url, method, path, payload:
                        updated.extend(payload["records"]) or {})
    fm.sync_model_settings(tmp_path, Lock())
    assert (effective_models(tmp_path)["repair_agent"] == "codex") is allowed
    if state == "in_use":
        assert not auth.auth_ready(tmp_path)  # Configuration permission never opens dispatch capacity.
        assert not any("暂不可用" in row["fields"].get("应用状态", "") for row in updated)
