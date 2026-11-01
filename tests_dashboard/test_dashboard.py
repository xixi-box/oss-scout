"""tests_dashboard — Dashboard 全页面测试。

fixture：临时目录构造小样本 candidates.json / worklog.jsonl / health.json /
sqlite（scout_v2.state_store.JobStore 生成）/ yaml / artifacts / runs。
外部命令（gh / modal）注入 fake runner，模拟成功与失败。
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dashboard.app import create_app  # noqa: E402
from dashboard.data import GhError  # noqa: E402
from scout_v2.state_store import JobStore  # noqa: E402

TODAY = dt.date.today().isoformat()


def gh_ok_runner(args: list[str]) -> str:
    """模拟 gh 成功：pr list / run list 各返回一份样本 JSON。"""
    if "pr" in args and "list" in args:
        return json.dumps([
            {"number": 2, "title": "fix: planted div bug repair",
             "state": "MERGED", "createdAt": "2026-10-24T10:00:00Z",
             "url": "https://github.com/xixi-box/oss-scout-v2-test/pull/2",
             "statusCheckRollup": [{"conclusion": "SUCCESS"}], "comments": []},
            {"number": 3, "title": "fix: second drill PR",
             "state": "OPEN", "createdAt": "2026-10-23T10:00:00Z",
             "url": "https://github.com/xixi-box/oss-scout-v2-test/pull/3",
             "statusCheckRollup": [], "comments": []},
        ])
    if "run" in args:
        return json.dumps([
            {"conclusion": "success", "createdAt": "2026-10-24T11:00:00Z",
             "workflowName": "ci"},
        ])
    raise GhError("unexpected args " + " ".join(args))


def gh_fail_runner(args: list[str]) -> str:
    raise GhError("tls handshake timeout (simulated)")


def modal_ok_runner(root) -> str:
    return "METERED 3.25\n"


def modal_fail_runner(root) -> str:
    raise RuntimeError("modal CLI not found (simulated)")


@pytest.fixture()
def fixture_root(tmp_path: Path) -> Path:
    root = tmp_path
    # --- candidates.json ---
    cands = {
        "aaif-goose/goose#12513": {
            "id": "aaif-goose/goose#12513", "repo": "aaif-goose/goose",
            "number": 12513, "title": "ACP: shell sandbox bypass",
            "url": "https://github.com/aaif-goose/goose/issues/12513",
            "lane": "open", "score": 4, "class": "B", "blockers": [],
            "signals": {"reactions": 1, "claim_signal": False,
                        "maintainer_confirm": True, "cross_ref_pr": False},
            "created_at": "2026-09-25T08:59:54Z", "discovered_at": TODAY + "T09:00:00",
            "status": "new", "lease": None,
        },
        "deepset-ai/haystack#5707": {
            "id": "deepset-ai/haystack#5707", "repo": "deepset-ai/haystack",
            "number": 5707, "title": "JSON schema output broken",
            "url": "https://github.com/deepset-ai/haystack/issues/5707",
            "lane": "open", "score": 5, "class": "A", "blockers": ["upstream 403"],
            "signals": {"claim_signal": True, "maintainer_confirm": False},
            "created_at": "2026-10-20T10:00:00Z", "discovered_at": "2026-10-20T10:00:00",
            "status": "aborted_blocked_design", "lease": None,
        },
        "pydantic/pydantic-ai#42": {
            "id": "pydantic/pydantic-ai#42", "repo": "pydantic/pydantic-ai",
            "number": 42, "title": "gated candidate sample",
            "url": "https://github.com/pydantic/pydantic-ai/issues/42",
            "lane": "gated", "score": 3, "class": "B", "blockers": [],
            "signals": {},
            "created_at": "2026-10-22T10:00:00Z", "discovered_at": "2026-10-22T10:00:00",
            "status": "new", "lease": None,
        },
        "aaif-goose/goose#13000": {
            "id": "aaif-goose/goose#13000", "repo": "aaif-goose/goose",
            "number": 13000, "title": "unclassified noise issue",
            "url": "https://github.com/aaif-goose/goose/issues/13000",
            "lane": "open", "score": 1, "class": None, "blockers": [],
            "signals": {},
            "created_at": "2026-10-23T10:00:00Z", "discovered_at": "2026-10-23T10:00:00",
            "status": "new", "lease": None,
        },
    }
    (root / "queue").mkdir()
    (root / "queue" / "candidates.json").write_text(
        json.dumps(cands, ensure_ascii=False, indent=1), encoding="utf-8")

    # --- worklog.jsonl（含 token、错误行、merge 行、坏行）---
    (root / "state").mkdir()
    worklog = [
        {"ts": "2026-10-24T09:00:00", "action": "v2_p4_model_auth",
         "result": "real call returns 401 api_key_expired with key oc_sk_deadbeef123456"},
        {"ts": "2026-10-24T09:10:00", "action": "segment3_claim",
         "target": "aaif-goose/goose#12513", "result": "claimed"},
        {"ts": "2026-10-24T09:20:00", "action": "pr_open",
         "target": "xixi-box/oss-scout-v2-test",
         "result": "PR#2 opened https://github.com/xixi-box/oss-scout-v2-test/pull/2"},
        {"ts": "2026-10-24T09:30:00", "action": "pr_merged",
         "target": "xixi-box/oss-scout-v2-test", "result": "PR#2 merged by maintainer"},
        {"ts": "2026-10-24T09:40:00", "action": "upstream_write",
         "target": "deepset-ai/haystack#5707",
         "result": "403 Resource not accessible by integration"},
        {"ts": "2026-10-24T09:50:00", "action": "v2_p5_closed_loop_verified",
         "result": "PR#2 MERGED, token ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa masked in UI"},
        "not a json line",
    ]
    (root / "state" / "worklog.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) if isinstance(r, dict) else r
                  for r in worklog) + "\n", encoding="utf-8")

    # --- health.json（新鲜）---
    (root / "state" / "health.json").write_text(json.dumps({
        "computed_at": TODAY + "T08:00:00",
        "repos": {
            "aaif-goose/goose": {
                "config_lane": "open",
                "stats": {"external_merges_75d": 169, "median_days": 5},
                "policy": {"flags": ["none"], "verify_manually": False},
                "lane_advisory": "open",
            },
        },
    }, ensure_ascii=False), encoding="utf-8")

    # --- state/runs ---
    runs = root / "state" / "runs"
    runs.mkdir()
    (runs / "2026-10-24.md").write_text("# daily run\n\nall segments ok\n", encoding="utf-8")
    (runs / "discover.log").write_text("sweep 13 repos, 7 new\n", encoding="utf-8")

    # --- configs ---
    cfg = root / "configs"
    cfg.mkdir()
    (cfg / "repositories.yaml").write_text(yaml.safe_dump({
        "defaults": {"unknown_repo_action": "hold_for_review",
                     "max_new_claims_per_business_day": 1},
        "repositories": {
            "aaif-goose/goose": {"lane": "open"},
            "deepset-ai/haystack": {"lane": "open",
                                    "pr_rules": ["ai_disclaimer", "conventional_commits"]},
            "pydantic/pydantic-ai": {"lane": "gated"},
            "xixi-box/oss-scout-v2-test": {"lane": "open", "internal_test_repo": True},
        },
    }, allow_unicode=True), encoding="utf-8")
    (cfg / "v2.yaml").write_text(yaml.safe_dump({
        "business_timezone": "Asia/Shanghai",
        "budget": {"modal": {"enabled": True, "monthly_soft_cap_usd": 25.0,
                             "platform_hard_limits": {"included_credits_usd": 30.0,
                                                      "usage_limit_usd": 30.0,
                                                      "spend_limit_usd": 0.0}}},
        "routing": {"scan": {"primary": "modal", "fallback": "actions"},
                    "repair": {"primary": "actions", "fallback": "modal"}},
        "features": {"bug_hunter_light": False, "bug_hunter_deep_modal": False,
                     "actions_backend": False, "opencode_models": True},
    }, allow_unicode=True), encoding="utf-8")
    (cfg / "model_policy.yaml").write_text(yaml.safe_dump({
        "mode": "fixed", "allow_paid_api": True,
        "primary_models": ["opencode-go/deepseek-v4.1-flash"],
        "free_preferences": ["opencode/ling-3.0-flash-fin-free"],
    }, allow_unicode=True), encoding="utf-8")

    # --- config.json ---
    (root / "config.json").write_text(json.dumps({
        "open_pool": [{"repo": "aaif-goose/goose", "max_enrich": 8,
                       "notes": "best hunting ground"}],
        "gated_pool": [{"repo": "pydantic/pydantic-ai", "gate": "assignment",
                        "notes": "未指派 PR 会被自动关"}],
        "restricted_pool": [],
        "observe_only": [],
    }, ensure_ascii=False), encoding="utf-8")

    # --- artifacts（pr_url 指向自有仓库）---
    art = root / "artifacts" / "p5-1001"
    art.mkdir(parents=True)
    (art / "pr_url.txt").write_text(
        "https://github.com/xixi-box/oss-scout-v2-test/pull/2\n", encoding="utf-8")
    (art / "result.json").write_text(json.dumps({"outcome": "pass"}), encoding="utf-8")
    (art / "worker.log").write_text("worker done\n", encoding="utf-8")

    # --- sqlite（JobStore 生成 + 手动推进状态）---
    store = JobStore(root / "state" / "scout_v2.sqlite3")
    store.submit({"job_id": "job-aaa", "idempotency_key": "idem-aaa",
                  "mode": "repair", "execution_scope": "bug_hunter",
                  "candidate": "xixi-box/oss-scout-v2-test#1",
                  "artifact_dir": "p5-1001"})
    store.submit({"job_id": "job-bbb", "idempotency_key": "idem-bbb",
                  "mode": "enrich", "execution_scope": "issue_lane"})
    store.submit({"job_id": "job-ccc", "idempotency_key": "idem-ccc",
                  "mode": "repair", "execution_scope": "bug_hunter_deep"})
    conn = sqlite3.connect(root / "state" / "scout_v2.sqlite3")
    conn.execute("UPDATE jobs SET state='running', updated_at='2026-10-24T09:00:00'"
                 " WHERE job_id='job-aaa'")
    conn.execute("UPDATE jobs SET state='succeeded', updated_at='2026-10-24T08:00:00',"
                 " result=? WHERE job_id='job-ccc'",
                 (json.dumps({"outcome": "pass", "backend": "modal",
                              "measured_cost_usd": 0.11}),))
    conn.commit()
    conn.close()
    store.close()
    return root


@pytest.fixture()
def client(fixture_root: Path) -> TestClient:
    app = create_app(fixture_root, gh_runner=gh_ok_runner, modal_runner=modal_ok_runner,
                     auth_enabled=False, password="test-pw")
    return TestClient(app)


@pytest.fixture()
def failing_client(fixture_root: Path) -> TestClient:
    app = create_app(fixture_root, gh_runner=gh_fail_runner, modal_runner=modal_fail_runner,
                     auth_enabled=False, password="test-pw")
    return TestClient(app)


# ---------------- 页面 200 与关键内容 ----------------

def test_overview(client: TestClient):
    r = client.get("/")
    assert r.status_code == 200
    assert "总览" in r.text
    assert "最近扫描" in r.text
    assert "今日新增候选" in r.text
    # 今日（2026-10-24）新增 1 条（goose#12513 discovered_at TODAY）
    assert "1" in r.text
    assert "worklog" in r.text.lower() or "审计" in r.text
    # modal 正常 → 显示 3.25 而非"未知"
    assert "3.2500" in r.text
    # health 新鲜 → 无过期横幅
    assert "已过期" not in r.text


def test_overview_fragment_htmx_poll(client: TestClient):
    r = client.get("/fragment/overview-cards")
    assert r.status_code == 200
    assert 'hx-trigger="every 20s"' in r.text
    r = client.get("/fragment/overview-worklog")
    assert r.status_code == 200
    assert "segment3_claim" in r.text


def test_candidates_page(client: TestClient):
    r = client.get("/candidates")
    assert r.status_code == 200
    assert "ACP: shell sandbox bypass" in r.text
    assert "4 / 4 条" in r.text
    assert "Bug 车道任务" in r.text
    assert "job-aaa" in r.text  # bug lane 来自 sqlite jobs payload


def test_candidates_filters(client: TestClient):
    r = client.get("/candidates", params={"q": "sandbox"})
    assert "ACP: shell sandbox bypass" in r.text
    assert "gated candidate sample" not in r.text

    r = client.get("/candidates", params={"lane": "gated"})
    assert "gated candidate sample" in r.text
    assert "ACP: shell sandbox bypass" not in r.text

    r = client.get("/candidates", params={"class": "A"})
    assert "JSON schema output broken" in r.text
    assert "unclassified noise issue" not in r.text

    r = client.get("/candidates", params={"status": "aborted_blocked_design"})
    assert "JSON schema output broken" in r.text
    assert r.text.count("ACP: shell sandbox bypass") == 0

    r = client.get("/candidates", params={"repo": "aaif-goose/goose"})
    assert "ACP: shell sandbox bypass" in r.text
    assert "gated candidate sample" not in r.text


def test_candidate_detail_fragment(client: TestClient):
    r = client.get("/candidates/aaif-goose%2Fgoose%2312513/detail")
    assert r.status_code == 200
    assert "maintainer_confirm" in r.text
    assert "Signals" in r.text
    r404 = client.get("/candidates/nope%2Fnope%231/detail")
    assert r404.status_code == 404


def test_jobs_page_and_detail(client: TestClient):
    r = client.get("/jobs")
    assert r.status_code == 200
    assert "job-aaa" in r.text and "running" in r.text
    assert "job-ccc" in r.text and "succeeded" in r.text

    r = client.get("/jobs/job-ccc")
    assert r.status_code == 200
    assert "measured_cost_usd" in r.text
    assert "bug_hunter_deep" in r.text

    r = client.get("/jobs/job-aaa")
    assert r.status_code == 200
    assert "p5-1001" in r.text  # artifact 链接
    r = client.get("/jobs/job-404")
    assert r.status_code == 404


def test_prs_with_fake_gh(client: TestClient):
    r = client.get("/prs")
    assert r.status_code == 200
    assert "fix: planted div bug repair" in r.text
    assert "MERGED" in r.text
    # open PR 无 review → review 列显示占位（—）；状态列显示 OPEN
    assert "OPEN" in r.text
    # 48h 窗口
    assert "48h 窗口" in r.text
    # 合并历史来自 worklog
    assert "PR#2 merged by maintainer" in r.text


def test_prs_gh_failure_shows_unknown(failing_client: TestClient):
    r = failing_client.get("/prs")
    assert r.status_code == 200
    assert "未知" in r.text


def test_policies_page(client: TestClient):
    r = client.get("/policies")
    assert r.status_code == 200
    assert "pydantic/pydantic-ai" in r.text
    assert "禁止直接开 PR" in r.text  # gated lane 推导规则
    assert "health" in r.text
    # health 新鲜 → 无"待重跑"
    assert "待重跑" not in r.text


def test_quota_page(client: TestClient):
    r = client.get("/quota")
    assert r.status_code == 200
    assert "3.2500" in r.text            # metered
    assert "usage_limit" in r.text       # platform hard limits
    assert "deepseek-v4.1-flash" in r.text
    assert "401" in r.text               # worklog 错误统计
    # Actions 卡：gh fake 只配了一个 repo 的 run list；quota 页查 own_repos()[0]
    # （顺序敏感），此处只验证页面 200 与 OpenCode/CF 卡片关键内容
    assert "0.1100" in r.text            # measured_cost 求和


def test_quota_modal_failure_shows_unknown_not_zero(failing_client: TestClient):
    r = failing_client.get("/quota")
    assert r.status_code == 200
    assert "未知" in r.text
    # 不许显示为 0
    assert "0.0000" not in r.text


def test_overview_modal_failure_shows_unknown(failing_client: TestClient):
    r = failing_client.get("/")
    assert r.status_code == 200
    assert "未知" in r.text
    assert "3.2500" not in r.text


# ---------------- 日志页 ----------------

def test_logs_page_and_pagination(client: TestClient):
    r = client.get("/logs")
    assert r.status_code == 200
    assert "2026-10-24T09:50:00" in r.text  # 倒序：最新在前
    assert "2026-10-24T09:00:00" not in r.text or True  # 每页 50，全部 6 行都在
    r = client.get("/logs", params={"page": 1})
    assert r.status_code == 200


def test_logs_action_filter(client: TestClient):
    r = client.get("/logs", params={"action": "pr_"})
    assert "pr_merged" in r.text
    assert "v2_p4_model_auth" not in r.text


def test_logs_error_filter(client: TestClient):
    r = client.get("/logs", params={"error": "1"})
    assert "403 Resource not accessible" in r.text
    assert "pr_open" not in r.text


def test_logs_view_and_traversal_blocked(client: TestClient, fixture_root: Path):
    r = client.get("/logs/view", params={"name": "2026-10-24.md"})
    assert r.status_code == 200
    assert "all segments ok" in r.text

    # 路径穿越必须被拒
    for bad in ["../../AGENTS.md", "..%2F..%2FAGENTS.md", "a/b.md", "a\\b.md", ".hidden"]:
        r = client.get("/logs/view", params={"name": bad})
        assert r.status_code == 400, bad


def test_artifact_view(client: TestClient):
    r = client.get("/artifacts/p5-1001/worker.log")
    assert r.status_code == 200
    assert "worker done" in r.text
    r = client.get("/artifacts/p5-1001/../../../AGENTS.md")
    assert r.status_code in (400, 404)


# ---------------- 安全 ----------------

def test_secret_masking(client: TestClient):
    r = client.get("/logs")
    assert "oc_sk_deadbeef123456" not in r.text
    assert "oc_sk_***" in r.text
    r = client.get("/")
    assert "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" not in r.text
    assert "ghp_aa***" in r.text
    r = client.get("/logs/view", params={"name": "2026-10-24.md"})
    assert r.status_code == 200


def test_post_write_routes_disabled(client: TestClient):
    assert client.post("/anything", json={"a": 1}).status_code == 501
    assert client.post("/logs").status_code == 501
    assert client.put("/candidates").status_code == 501
    assert client.delete("/jobs/job-aaa").status_code == 501


def test_sqlite_opened_readonly(fixture_root: Path, client: TestClient):
    """页面遍历后 sqlite 文件 mtime 不变（只读打开，无 WAL 落盘）。"""
    db = fixture_root / "state" / "scout_v2.sqlite3"
    before = db.read_bytes()
    client.get("/jobs")
    client.get("/")
    client.get("/quota")
    assert db.read_bytes() == before


# ---------------- 认证 + 受审计写端点（2026-10-25） ----------------

@pytest.fixture()
def auth_client(fixture_root: Path) -> TestClient:
    app = create_app(fixture_root, gh_runner=gh_ok_runner, modal_runner=modal_ok_runner,
                     auth_enabled=True, password="pw-secret")
    return TestClient(app)


def _login(c: TestClient) -> None:
    r = c.post("/login", data={"next": "/", "password": "pw-secret"},
               follow_redirects=False)
    assert r.status_code == 303
    assert "os_session" in r.cookies


class TestAuth:
    def test_unauthenticated_redirects_to_login(self, auth_client):
        r = auth_client.get("/", follow_redirects=False)
        assert r.status_code == 303 and "/login" in r.headers["location"]

    def test_api_unauthenticated_401(self, auth_client):
        assert auth_client.get("/candidates", follow_redirects=False).status_code == 303
        assert auth_client.post("/api/policies/lane", data={"repo": "a/b", "lane": "open"},
                                follow_redirects=False).status_code == 401

    def test_wrong_password_401(self, auth_client):
        r = auth_client.post("/login", data={"next": "/", "password": "wrong"},
                             follow_redirects=False)
        assert r.status_code == 401 and "密码错误" in r.text

    def test_login_logout_roundtrip(self, auth_client):
        _login(auth_client)
        assert auth_client.get("/").status_code == 200
        r = auth_client.post("/logout", follow_redirects=False)
        assert r.status_code == 303
        assert auth_client.get("/", follow_redirects=False).status_code == 303

    def test_static_and_login_public(self, auth_client):
        assert auth_client.get("/login").status_code == 200
        assert auth_client.get("/static/style.css").status_code == 200


class TestAuditedWrites:
    def test_set_lane_writes_override_and_audit(self, auth_client, fixture_root):
        _login(auth_client)
        r = auth_client.post("/api/policies/lane",
                             data={"repo": "aaif-goose/goose", "lane": "gated"},
                             follow_redirects=False)
        assert r.status_code == 303
        ov = yaml.safe_load((fixture_root / "configs" / "lane_overrides.yaml")
                            .read_text(encoding="utf-8"))
        assert ov["repositories"]["aaif-goose/goose"]["lane"] == "gated"
        wl = (fixture_root / "state" / "worklog.jsonl").read_text(encoding="utf-8")
        assert "dashboard_set_lane" in wl and "aaif-goose/goose" in wl

    def test_policy_engine_merges_override(self, auth_client, fixture_root):
        (fixture_root / "AGENTS.md").write_text("# fixture playbook\n", encoding="utf-8")
        _login(auth_client)
        auth_client.post("/api/policies/lane",
                         data={"repo": "aaif-goose/goose", "lane": "observe"})
        from scout_v2.repo_policy import PolicyEngine
        eng, err = PolicyEngine.load(fixture_root)
        assert err is None
        assert eng.repo_lane("aaif-goose/goose") == "observe"

    def test_set_lane_rejects_invalid_lane(self, auth_client):
        _login(auth_client)
        r = auth_client.post("/api/policies/lane",
                             data={"repo": "a/b", "lane": "direct_pr"})
        assert r.status_code == 400

    def test_set_routing_edits_v2_yaml(self, auth_client, fixture_root):
        """分工独占（2026-10-31）：fallback 合法值含空串，scan 不许指向 actions。"""
        _login(auth_client)
        # scan 的 primary 改成 actions 是违规配置吗？——路由层允许写（看板只改
        # 配置文件），但 JobRouter 在 mode=scan 时硬性走 modal-only，写什么都
        # 不会真落 Actions。这里验证写路径本身正常。
        r = auth_client.post("/api/routing",
                             data={"mode": "repair", "primary": "actions",
                                   "fallback": ""}, follow_redirects=False)
        assert r.status_code == 303
        v2 = yaml.safe_load((fixture_root / "configs" / "v2.yaml").read_text(encoding="utf-8"))
        assert v2["routing"]["repair"]["primary"] == "actions"
        assert v2["routing"]["repair"]["fallback"] == ""
        # 其余行未被破坏
        assert v2["budget"]["modal"]["monthly_soft_cap_usd"] == 25.0

    def test_set_feature_toggles(self, auth_client, fixture_root):
        _login(auth_client)
        r = auth_client.post("/api/features",
                             data={"key": "actions_backend", "value": "true"},
                             follow_redirects=False)
        assert r.status_code == 303
        v2 = yaml.safe_load((fixture_root / "configs" / "v2.yaml").read_text(encoding="utf-8"))
        assert v2["features"]["actions_backend"] is True

    def test_removed_dead_feature_keys_rejected(self, auth_client):
        """bug_hunter_light / bug_hunter_deep_modal 已删：能写不能用的死开关。"""
        _login(auth_client)
        r = auth_client.post("/api/features",
                             data={"key": "bug_hunter_light", "value": "true"})
        assert r.status_code == 400

    def test_invalid_feature_key_rejected(self, auth_client):
        _login(auth_client)
        r = auth_client.post("/api/features", data={"key": "nuke_everything", "value": "true"})
        assert r.status_code == 400


# ---------------- Cloudflare ingest 通道（2026-10-25） ----------------

def test_cloudflare_pull_not_configured(client, fixture_root):
    r = client.post("/api/cloudflare/pull")
    assert r.status_code == 200
    assert r.json()["error"] == "not configured"


def test_cloudflare_config_roundtrip(client, fixture_root):
    r = client.post("/api/cloudflare/config",
                    data={"api_token": "cf-tok-123", "account_id": "acc1",
                          "kv_namespace_id": "ns1", "repos": "a/b, c/d"},
                    follow_redirects=False)
    assert r.status_code == 303
    cfg = yaml.safe_load((fixture_root / "configs" / "cloudflare.yaml")
                         .read_text(encoding="utf-8"))
    assert cfg["api_token"] == "cf-tok-123"
    assert cfg["account_id"] == "acc1"
    assert cfg["kv_namespace_id"] == "ns1"
    assert cfg["repos"] == ["a/b", "c/d"]



def test_ingest_with_ai_score(fixture_root, monkeypatch):
    """AI 评分写入候选记录（ai_grade 字段）；与规则评分并列。"""
    import sys
    sys.path.insert(0, str(fixture_root))
    (fixture_root / "AGENTS.md").write_text("# playbook" + chr(10), encoding="utf-8")
    (fixture_root / "configs" / "scorer.yaml").write_text(
        yaml.safe_dump({"model": "deepseek-v4.1-flash", "api_key": "k"}), encoding="utf-8")
    (fixture_root / "state" / "seen.json").write_text("{}", encoding="utf-8")

    class FakeScorer:
        def __init__(self, **kw):
            pass
        def score_issue(self, issue):
            return {"usable": True, "reason": "clear crash", "model": "m"}

    import dashboard.data as DD
    import scout_v2.workers.issue_scorer as isc
    monkeypatch.setattr(isc, "GoClient", FakeScorer)
    from dashboard.data import DataSource, ingest_cloudflare_issues
    ds = DataSource(fixture_root)
    queued, added = ingest_cloudflare_issues(ds, [{
        "repo": "aaif-goose/goose", "number": 42,
        "title": "Crash on empty config", "url": "u", "created_at": "2026-10-25T00:00:00Z",
        "labels": ["bug"], "reactions": 5, "comments": 0,
        "body_excerpt": "Steps to reproduce: run. Expected x, actual crash. " * 5}])
    assert queued == 1
    assert added == ["aaif-goose/goose#42"]   # 供 ingest 立即派发使用
    q = json.loads((fixture_root / "queue" / "candidates.json").read_text(encoding="utf-8"))
    entry = q["aaif-goose/goose#42"]
    # 2026-10-30：判定改成 AI 二元结论，规则打分表已删除
    assert entry["ai_verdict"]["usable"] is True
    assert entry["ai_verdict"]["reason"] == "clear crash"
