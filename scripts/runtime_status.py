"""Read-only deployment checks. Prints counts and status, never credentials."""
from __future__ import annotations

import argparse
import collections
import json
import os
import sqlite3
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scout_v2.state_store import business_now


def inspect(root: Path) -> dict:
    config = yaml.safe_load((root / "configs/repositories.yaml").read_text(encoding="utf-8"))
    repos = {k: v for k, v in config["repositories"].items() if not v.get("internal")}
    queue = json.loads((root / "queue/candidates.json").read_text(encoding="utf-8"))
    now = business_now()
    result = {"business_time": now.isoformat(), "repositories": len(repos),
              "daily_cap": config["defaults"]["max_new_claims_per_business_day"],
              "queue": dict(collections.Counter(e.get("status") for e in queue.values()))}
    for filename, key, query, args in [
        ("scout_v2.sqlite3", "starts_today", "SELECT COUNT(*) FROM claim_days WHERE day=?", (now.date().isoformat(),)),
        ("scout_v2.sqlite3", "published_today", "SELECT COUNT(*) FROM publications WHERE day=?", (now.date().isoformat(),)),
        ("scout_v2.sqlite3", "pending_publication", "SELECT COUNT(*) FROM publications WHERE day IS NULL", ()),
        ("issue_inbox.sqlite3", "pending_screening", "SELECT COUNT(*) FROM inbox WHERE done_version<version", ()),
    ]:
        db = root / "state" / filename
        if db.exists():
            with sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True) as conn:
                try:
                    result[key] = conn.execute(query, args).fetchone()[0]
                except sqlite3.OperationalError as exc:
                    if "no such table: publications" not in str(exc):
                        raise
                    result[key] = 0
        else:
            result[key] = 0
    latest = {}
    path = root / "state/worklog.jsonl"
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            action = str(record.get("action") or "")
            if action.startswith("dashboard_bitable_sync"):
                latest[action] = {"at": record.get("ts"), "result": str(record.get("result"))[:300]}
    result["table_sync"] = latest
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(os.environ.get("OSSCOUT_DASHBOARD_ROOT", "/data")))
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--screening", action="store_true")
    args = parser.parse_args()
    result = inspect(args.root)
    if args.screening:
        from scout_v2.workers.openjev import OpenJevClient
        sample = {"title": "Synthetic parser crash with empty input",
                  "body": "The attached two-line reproduction makes parse([]) raise IndexError. "
                          "Expected an empty result. A focused unit test covers this path.",
                  "comments": [], "linked_pull_requests": []}
        result["screening_probe"] = OpenJevClient().score_issue(sample)
    if args.worker:
        import urllib.request
        cfg = yaml.safe_load((args.root / "configs/cloudflare.yaml").read_text(encoding="utf-8"))
        token = os.environ.get("CF_API_TOKEN") or cfg.get("api_token")
        prefix = (f"https://api.cloudflare.com/client/v4/accounts/{cfg['account_id']}"
                  f"/storage/kv/namespaces/{cfg['kv_namespace_id']}/values/")
        for key in ("diag", "watermarks"):
            req = urllib.request.Request(prefix + key, headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(req, timeout=20) as response:
                payload = json.loads(response.read())
            result["worker_" + key] = len(payload) if key == "watermarks" else payload
    print(json.dumps(result, ensure_ascii=False, indent=2))
    assert result["repositories"] == 50, "repository count changed"
    assert result["daily_cap"] == 5, "daily cap drift"
    if args.screening:
        assert result["screening_probe"]["usable"] is not None, "OpenJev screening probe failed"
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
