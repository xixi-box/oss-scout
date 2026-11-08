"""Read-only smoke test for the deployed controller and Feishu configuration."""

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scout_v2 import feishu_bitable as fb, feishu_models as fm
from scout_v2.console_models import effective_models
from scout_v2.state_store import CANDIDATE_LIMIT, WAITING_CANDIDATE_STATES, active_repair_keys


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
    candidates = json.loads((root / "queue/candidates.json").read_text(encoding="utf-8"))
    waiting = {key.lower() for key, entry in candidates.items() if entry.get("status") in WAITING_CANDIDATE_STATES}
    active = waiting | active_repair_keys(root, candidates)
    assert len(active) <= CANDIDATE_LIMIT, f"Candidate pool exceeds {CANDIDATE_LIMIT}: {len(active)}"
    for _ in range(8):
        table_id = fb.load_binding(root).get("model_table_id")
        if table_id:
            try:
                records = fb.load_records(fb._binding_app_token(root), table_id)
            except Exception:
                raise RuntimeError("Unable to read Feishu model settings") from None
            expected = fm.PROVIDERS.keys()
            if (expected == records.keys() and all(records[key]["fields"].get("模型名") for key in expected)
                    and records["OpenCode"]["fields"].get("Base URL")):
                break
        time.sleep(5)
    else:
        raise AssertionError("Feishu model settings table is not ready")
    models = effective_models(root)
    base = fm._field_value(records["OpenCode"]["fields"].get("Base URL"), "opencode_base")
    assert base == models["opencode_base"], "OpenCode Base URL has not synchronized"
    keys_present = {}
    for provider, key in (("OpenCode", "opencode_key"), ("Codiv", "codiv_key")):
        actual = records[provider]["fields"].get("API 密钥") or ""
        if actual != models[key]:
            raise AssertionError(f"{provider} credential has not synchronized")
        keys_present[provider] = bool(actual)
    print(json.dumps({"feishu_model_table_id": table_id, "model_settings_rows": len(expected),
                      "credentials_present": keys_present, "opencode_base_synced": True}))
    print(json.dumps({"controller": "verified", "active_candidates": len(active),
                      "pr_table_redirect": True}))


if __name__ == "__main__":
    main()
