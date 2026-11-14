"""Read-only smoke test for the deployed controller and Feishu configuration."""

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scout_v2 import feishu_bitable as fb, feishu_models as fm
from scout_v2.console_models import effective_models, runtime_limit
from scout_v2.state_store import STAGED_CANDIDATE_LIMIT, staged_candidate_count


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def main():
    root = Path(os.environ.get("OSSCOUT_DASHBOARD_ROOT", "/data"))
    base_url = "http://127.0.0.1:8787"
    with urllib.request.urlopen(base_url + "/healthz", timeout=10) as response:
        assert json.load(response) == {"ok": True}
    try:
        urllib.request.build_opener(NoRedirect).open(base_url + "/", timeout=10)
    except urllib.error.HTTPError as exc:
        assert exc.code == 303 and exc.headers["Location"].startswith("https://my.feishu.cn/base/")
        assert "?table=tbl" in exc.headers["Location"]
    else:
        raise AssertionError("PR table redirect is missing")
    for route in ("/feishu/callback", "/api/ingest/pr-comments"):
        request = urllib.request.Request(base_url + route, data=b"{}", headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(request, timeout=10)
        except urllib.error.HTTPError as exc:
            assert exc.code == 404, f"Retired PR endpoint still responds: {route} {exc.code}"
        else:
            raise AssertionError(f"Retired PR endpoint is still active: {route}")
    assert not (Path(__file__).resolve().parents[1] / "scout_v2/pr_approval.py").exists()
    candidates = json.loads((root / "queue/candidates.json").read_text(encoding="utf-8"))
    staged = staged_candidate_count(candidates)
    limit = runtime_limit(root, "candidate_pool_size", STAGED_CANDIDATE_LIMIT)
    assert staged <= limit, f"Staged backlog exceeds {limit}: {staged}"
    for _ in range(8):
        table_id = fb.load_binding(root).get("model_table_id")
        if table_id:
            try:
                records = fb.load_records(fb._binding_app_token(root), table_id)
            except Exception:
                raise RuntimeError("Unable to read Feishu model settings") from None
            expected = fm.PROVIDERS.keys()
            if (fb.load_binding(root).get("model_table_layout") == 6
                    and expected == records.keys() and all(records[key]["fields"].get("模型名")
                    for key, columns in fm.PROVIDERS.items() if "模型名" in columns)
                    and records["OpenCode"]["fields"].get("Base URL")
                    and all(records["运行设置"]["fields"].get(column)
                            for column in ("候选池大小", "每日 PR 额度"))):
                break
        time.sleep(5)
    else:
        raise AssertionError("Feishu model settings table is not ready")
    table_url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{fb._binding_app_token(root)}/tables/{table_id}"
    columns = fm._table_request(table_url, "GET", "fields?page_size=100")
    assert not {"PR 评论触发", "回复模型"} & {f["field_name"] for f in columns.get("items", [])}
    assert fb.PR_RECORD_SYNC_SECONDS == 300 and fb.FULL_TABLE_SYNC_SECONDS == 900
    from scout_v2.codex_auth import auth_ready, exchange
    concurrent_auth = False
    if auth_ready(root):
        leases = []
        try:
            for _ in range(2):
                payload, status = exchange(root, {"operation": "acquire", "protocol": 2})
                assert status == 200 and payload.get("protocol") == 2, "Concurrent Codex lease failed"
                leases.append(payload["lease"])
                assert payload["auth"]["tokens"]["refresh_token"].startswith("oss-scout-lease:")
            assert len(set(leases)) == 2
            concurrent_auth = True
        finally:
            for lease in leases:
                exchange(root, {"operation": "release", "protocol": 2, "lease": lease})
    models = effective_models(root)
    base = fm._field_value(records["OpenCode"]["fields"].get("Base URL"), "opencode_base")
    assert base == models["opencode_base"], "OpenCode Base URL has not synchronized"
    running = records["运行设置"]["fields"]
    for column, key in (("候选池大小", "candidate_pool_size"), ("每日 PR 额度", "daily_pr_quota")):
        assert fm._field_value(running.get(column), key) == models[key], f"{key} has not synchronized"
    keys_present = {}
    for provider, key in (("OpenCode", "opencode_key"), ("Codiv", "codiv_key")):
        actual = records[provider]["fields"].get("API 密钥") or ""
        if actual != models[key]:
            raise AssertionError(f"{provider} credential has not synchronized")
        keys_present[provider] = bool(actual)
    print(json.dumps({"feishu_model_table_id": table_id, "model_settings_rows": len(expected),
                      "credentials_present": keys_present, "opencode_base_synced": True}))
    print(json.dumps({"controller": "verified", "staged_candidates": staged,
                      "candidate_pool_size": limit, "daily_pr_quota": int(models["daily_pr_quota"]),
                      "pr_table_redirect": True, "pr_followup_removed": True, "retired_settings_removed": True, "pr_record_sync_seconds": fb.PR_RECORD_SYNC_SECONDS,
                      "codex_concurrent_leases_verified": concurrent_auth}))


if __name__ == "__main__":
    main()
