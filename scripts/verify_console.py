"""Read-only smoke test for the deployed console and the existing Feishu link."""

import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dashboard.app import SESSION_COOKIE, _make_session


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def main():
    root = Path(os.environ.get("OSSCOUT_DASHBOARD_ROOT", "/data"))
    key = (root / "state/dashboard_session_key").read_text(encoding="utf-8").strip().encode()
    headers = {"Cookie": f"{SESSION_COOKIE}={_make_session(key)}"}

    def request(path):
        return urllib.request.Request("http://127.0.0.1:8787" + path, headers=headers)

    with urllib.request.urlopen(request("/console-demo"), timeout=10) as response:
        assert "工作中" in response.read().decode()
    with urllib.request.urlopen(request("/api/console-demo/activity"), timeout=90) as response:
        data = json.load(response)
    assert len(data["rows"]) == 60
    try:
        urllib.request.build_opener(NoRedirect).open(request("/console/pr-table"), timeout=10)
    except urllib.error.HTTPError as exc:
        assert exc.code == 303 and exc.headers["Location"].startswith("https://my.feishu.cn/base/")
        assert "?table=tbl" in exc.headers["Location"]
    else:
        raise AssertionError("PR table redirect is missing")
    print(json.dumps({"console": "verified", "running": len(data["working"]),
                      "queue": len(data["queue"]), "published_today": len(data["today"]),
                      "repair_records": len(data["repairs"]), "schedule": data["schedule"],
                      "workflows_available": data["working_available"],
                      "pr_table_redirect": True}))


if __name__ == "__main__":
    main()
