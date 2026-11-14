import json
import os

from fastapi.testclient import TestClient

os.environ.setdefault("OSSCOUT_DISABLE_AUTO_REPAIR", "1")

from dashboard.app import create_app
from dashboard.console_activity import candidate_activity
from dashboard.data import DataSource
from scout_v2.state_store import ClaimLedger, business_now


def test_feishu_uses_started_runs_and_verified_publications(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    db = ClaimLedger(state / "scout_v2.sqlite3")
    assert db.reserve_publication("Asia/Shanghai", 5, "job-1", "traefik/traefik",
                                  "xixi-box:fix/issue-12-1234567-12345678", "xixi-box/oss-scout")
    db.set_publication_run("job-1", "123")
    issue_run = dict(id=123, display_title="repair job-1", status="queued")

    def gh(args):
        assert args[-1].endswith("/123")
        return json.dumps(issue_run)

    assert candidate_activity(DataSource(tmp_path, gh_runner=gh)) == (set(), {}, {})
    now = business_now()
    issue_run.update(status="in_progress", run_started_at=now.isoformat())
    assert candidate_activity(DataSource(tmp_path, gh_runner=gh)) == ({"traefik/traefik#12"}, {}, {})

    url = "https://github.com/traefik/traefik/pull/13"
    db.finish_publication("job-1", day=now.date().isoformat(), url=url)
    assert candidate_activity(DataSource(tmp_path, gh_runner=gh)) == (set(), {"traefik/traefik#12": url}, {})
    db.close()


def test_service_updates_queue_schedule_and_opens_existing_feishu_table(tmp_path, monkeypatch):
    monkeypatch.delenv("OSSCOUT_DASHBOARD_ROOT", raising=False)
    monkeypatch.delenv("FEISHU_BITABLE_APP_TOKEN", raising=False)
    root = tmp_path
    for directory in ("state", "configs", "queue"):
        (root / directory).mkdir()
    (root / "state/feishu_bitable.json").write_text(
        json.dumps({"app_token": "baseExample", "pr_table_id": "tblExample"}))
    (root / "queue/candidates.json").write_text(json.dumps({
        "traefik/traefik#12": {"status": "staged", "defer_reason": "daily_quota", "staged_at": business_now().isoformat(),
                              "eligible_at": "2099-11-08T04:00:00+08:00"},
        "traefik/traefik#13": {"status": "staged"},
    }))
    client = TestClient(create_app(root, auth_enabled=False))
    table = client.get("/", follow_redirects=False)
    assert table.headers["location"] == "https://my.feishu.cn/base/baseExample?table=tblExample"
    result = client.post("/api/console/schedule", json={"time": "05:27"})
    assert result.json() == {"time": "05:27"}
    queue = json.loads((root / "queue/candidates.json").read_text(encoding="utf-8"))
    assert queue["traefik/traefik#12"]["eligible_at"] == "2099-11-08T05:27:00+08:00"
    assert client.post("/api/console/schedule", json={"time": "99:00"}).status_code == 400


def test_console_model_save_keeps_secrets_out_of_api(tmp_path, monkeypatch):
    monkeypatch.delenv("OSSCOUT_DASHBOARD_ROOT", raising=False)
    client = TestClient(create_app(tmp_path, auth_enabled=False))
    values = dict(repair_model="opencode-go/example", reply_model="example-reply",
                  codiv_base="https://api.codiv.ai", codiv_model="openjev-latest",
                  codiv_key="fake-codiv-key")
    assert client.post("/api/console/models", json=values).json() == {"ok": True}
    response = client.get("/api/console/models")
    assert response.json()["codiv_configured"] is True
    assert response.json()["repair_model"] == values["repair_model"]
    assert response.json()["reply_model"] == values["reply_model"]
    assert values["codiv_key"] not in response.text
    assert client.post("/api/console/models", json={
        **values, "codiv_base": "https://127.0.0.1/"
    }).status_code == 400
