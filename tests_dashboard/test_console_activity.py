import json

from fastapi.testclient import TestClient

from dashboard.app import create_app
from dashboard.console_activity import candidate_activity, snapshot
from dashboard.data import DataSource
from scout_v2.state_store import ClaimLedger, business_now


def test_console_uses_started_runs_and_verified_publications(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    db = ClaimLedger(state / "scout_v2.sqlite3")
    assert db.reserve_publication("Asia/Shanghai", 5, "job-1", "docker/compose",
                                  "xixi-box:fix/issue-12-1234567-12345678", "xixi-box/oss-scout")
    db.set_publication_run("job-1", "123")
    (state / "pr_fix_attempted.json").write_text(json.dumps({"istio/istio#24@abc": {}}))
    issue_run = dict(id=123, display_title="repair job-1", status="queued")
    fix_run = dict(id=456, display_title="prfix-istio-istio-24-abc", status="queued")

    def gh(args):
        if args[-1].endswith("/123"):
            return json.dumps(issue_run)
        assert "oss-scout-pr-fix.yml" in args[-1]
        return json.dumps(dict(workflow_runs=[fix_run]))

    def activity():
        return snapshot(DataSource(tmp_path, gh_runner=gh))

    assert activity()["working"] == []
    assert candidate_activity(DataSource(tmp_path, gh_runner=gh)) == (set(), {})
    now = business_now()
    for run in (issue_run, fix_run):
        run.update(status="in_progress", run_started_at=now.isoformat())
    assert candidate_activity(DataSource(tmp_path, gh_runner=gh)) == ({"docker/compose#12"}, {})
    working = activity()["working"]
    assert [(r["kind"], r["number"]) for r in working] == [("created", 12), ("repair", 24)]
    assert working[0]["workflow"].endswith("/actions/runs/123")
    assert working[1]["workflow"].endswith("/actions/runs/456")

    url = "https://github.com/docker/compose/pull/13"
    db.finish_publication("job-1", day=now.date().isoformat(), url=url)
    events = [
        dict(action="pipeline_pr_open", result=f"{url} (auto pipeline)", ts=now.isoformat()),
        dict(action="dashboard_pr_fix_result", target="istio/istio#24",
             result="pushed=True test=passed", ts=now.isoformat()),
        dict(action="dashboard_pr_fix_result", target="docker/compose#99",
             result="pushed=False test=failed", ts=now.isoformat()),
    ]
    (state / "worklog.jsonl").write_text("\n".join(map(json.dumps, events)), encoding="utf-8")
    assert candidate_activity(DataSource(tmp_path, gh_runner=gh)) == (set(), {"docker/compose#12": url})
    finished = activity()
    assert finished["working"] == []
    assert [r["number"] for r in finished["today"]] == [13]
    assert [r["number"] for r in finished["repairs"]] == [24]
    db.close()


def test_console_reads_real_queue_and_opens_existing_table(tmp_path, monkeypatch):
    monkeypatch.delenv("OSSCOUT_DASHBOARD_ROOT", raising=False)
    monkeypatch.delenv("FEISHU_BITABLE_APP_TOKEN", raising=False)
    root = tmp_path
    for directory in ("state", "configs", "queue"):
        (root / directory).mkdir()
    (root / "state/feishu_bitable.json").write_text(
        json.dumps({"app_token": "baseExample", "pr_table_id": "tblExample"}))
    (root / "queue/candidates.json").write_text(json.dumps({
        "docker/compose#12": {"status": "staged", "staged_at": business_now().isoformat(),
                              "eligible_at": "2099-11-08T04:00:00+08:00"},
        "docker/compose#13": {"status": "new"},
    }))
    client = TestClient(create_app(root, auth_enabled=False))
    assert client.get("/console-demo").status_code == 200
    table = client.get("/console/pr-table", follow_redirects=False)
    assert table.headers["location"] == "https://my.feishu.cn/base/baseExample?table=tblExample"
    result = client.post("/api/console/schedule", json={"time": "05:27"})
    assert result.json() == {"time": "05:27"}
    data = client.get("/api/console-demo/activity").json()
    assert len(data["rows"]) == 60
    assert data["schedule"] == "05:27"
    assert [(item["number"], item["eligible_at"]) for item in data["queue"]] == [
        (12, "2099-11-08T05:27:00+08:00")
    ]
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
