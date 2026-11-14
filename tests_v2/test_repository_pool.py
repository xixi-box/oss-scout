"""Production roster boundaries: honorary membership does not disable contribution."""
from collections import Counter
import json
from pathlib import Path
import sqlite3
import tomllib

import pytest
import yaml

from scout_v2 import feishu_bitable as fb
from scout_v2.git_host import assignment_facts
from scout_v2.repo_policy import PolicyEngine
from scout_v2.roster import monitored_repo
from scripts.monitor_prs import build_repo_rows
from scripts.runtime_status import verify_feishu_repos

ROOT = Path(__file__).resolve().parents[1]


def test_exact_pool_and_discovery_boundaries():
    config = yaml.safe_load((ROOT / "configs/repositories.yaml").read_text(encoding="utf-8"))
    external = {k: v for k, v in config["repositories"].items() if not v.get("internal")}
    variables = tomllib.loads((ROOT / "cloudflare/wrangler.toml").read_text(encoding="utf-8"))["vars"]
    assert Counter(e["pool"] for e in external.values()) == {"formal": 50, "honor": 8}
    assert set(variables["TRACKED_REPOS"].split(",")) == set(external)
    assert set(variables["REPOS"].split(",")) == {r for r, e in external.items() if e["issue_scan"]}
    assert len(variables["REPOS"].split(",")) == 57
    policy, err = PolicyEngine.load(ROOT)
    assert not err
    for repo, entry in external.items():
        assert monitored_repo(repo)
        assert policy.repo_lane(repo) == ("open" if entry["issue_scan"] else "observe")
        assert policy.pr_invited(repo, set())
    assert policy.repo_lane("clickhouse/clickhouse") == "observe"
    assert monitored_repo("ClickHouse/ClickHouse")  # Keep existing PR follow-up.
    assert monitored_repo("jlowin/fastmcp") and monitored_repo("PrefectHQ/fastmcp")
    assert not monitored_repo("docker/compose")  # Removed from this version.


def test_fastmcp_assignment_or_explicit_exemption():
    policy, _ = PolicyEngine.load(ROOT)
    rule = policy.pr_assignment("PrefectHQ/fastmcp")
    assert assignment_facts({}, rule)["requires_assignment"]
    assert not assignment_facts({}, rule)["assignment_exempt"]
    assert assignment_facts({"labels": [{"name": "prs welcome"}]}, rule)["assignment_exempt"]
    assert assignment_facts({"assignees": [{"login": "xixi-box"}]}, rule)["assigned_to_me"]


def test_feishu_verifies_membership_and_paused_state(monkeypatch, tmp_path):
    rows = build_repo_rows(ROOT)
    records = [{"fields": fb.repo_row_to_fields(row)} for row in rows]
    monkeypatch.setattr(fb, "configured", lambda _: True)
    monkeypatch.setattr(fb, "load_binding", lambda _: {"repo_table_id": "table"})
    monkeypatch.setattr(fb, "_binding_app_token", lambda _: "fake")
    monkeypatch.setattr(fb, "record_pages", lambda *_: iter(records))
    assert verify_feishu_repos(tmp_path) == {
        "rows": 58, "exact_match": True, "formal": 50, "honor": 8, "issue_scanning": 57}
    clickhouse = next(r for r in records if "clickhouse" in fb._key_label(r["fields"]["键"]).lower())
    clickhouse["fields"]["参与状态"] = "扫描与贡献"
    with pytest.raises(SystemExit, match="field mismatch"):
        verify_feishu_repos(tmp_path)


@pytest.mark.parametrize("busy", ["dispatcher", "running", "publication"])
def test_deploy_waits_without_interrupting_repair(monkeypatch, tmp_path, busy):
    import subprocess
    import sys
    import time

    (tmp_path / "state").mkdir()
    (tmp_path / "queue").mkdir()
    queue = tmp_path / "queue/candidates.json"
    queue.write_text(json.dumps({"a/b#1": {"status": "running" if busy == "running" else "staged"}}))
    db = sqlite3.connect(tmp_path / "state/scout_v2.sqlite3")
    db.execute("CREATE TABLE publications (day TEXT)")
    if busy == "publication":
        db.execute("INSERT INTO publications VALUES (NULL)")
    db.commit()
    lease = sqlite3.connect(tmp_path / "state/dispatcher.sqlite3", isolation_level=None)
    if busy == "dispatcher":
        lease.execute("BEGIN IMMEDIATE")
    waited = []
    deployed = []

    def finish_existing_task(seconds):
        assert not deployed
        waited.append(seconds)
        lease.rollback()
        queue.write_text(json.dumps({"a/b#1": {"status": "pr_open"}}))
        db.execute("UPDATE publications SET day='2026-11-14'")
        db.commit()

    monkeypatch.setattr(time, "sleep", finish_existing_task)
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: deployed.append(args))
    monkeypatch.setattr(sys, "argv", ["-", "compose.yaml"])
    workflow = yaml.safe_load((ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8"))
    scripts = [s.get("run", "") for j in workflow["jobs"].values() for s in j["steps"]]
    script = next(s for s in scripts if "Active repair or pending publication" in s)
    source = script.split("<<'PY'", 1)[1].split("\n", 1)[1].split("\nPY", 1)[0]
    source = source.replace('Path("/opt/oss-scout-data")', f"Path({str(tmp_path)!r})")
    try:
        exec(compile(source, "deploy-guard", "exec"), {})
        assert waited == [10]
        assert deployed == [["docker", "compose", "-f", "compose.yaml", "up", "-d", "--remove-orphans"]]
    finally:
        lease.close()
        db.close()
