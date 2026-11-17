"""One-time, trusted controller operation; drain jobs without cancelling them."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

from codex_secret_session import ENVIRONMENT, REPO, SECRET, STATE, digest, gh, validated


def main():
    root = Path(os.environ.get("OSSCOUT_DASHBOARD_ROOT", "/data"))
    marker = root / "state/codex_auth_transport.json"
    lock = sqlite3.connect(root / "state/dispatcher.sqlite3", timeout=1, isolation_level=None)
    try:
        for _ in range(660):
            try:
                if not lock.in_transaction:
                    lock.execute("BEGIN IMMEDIATE")
                queue = json.loads((root / "queue/candidates.json").read_text())
                with sqlite3.connect(f"file:{root}/state/scout_v2.sqlite3?mode=ro", uri=True) as db:
                    pending = db.execute("SELECT COUNT(*) FROM publications WHERE day IS NULL").fetchone()[0]
                if pending or any(e.get("status") == "running" for e in queue.values()):
                    print("Waiting for existing remote jobs and publication reconciliation", flush=True)
                else:
                    break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise
            time.sleep(10)
        else:
            raise RuntimeError("Migration postponed while existing repairs finish")
        if marker.exists():
            raise RuntimeError("Transport already selected; refusing old credential reseed")
        from scout_v2.codex_auth import _broker_lock, _active_leases, _load, auth_path
        with _broker_lock(root):
            now = time.time()
            if _active_leases(root, now) or _load(auth_path(root).parent / "lease.json").get("expires", 0) > now:
                raise RuntimeError("Legacy credential lease still active")
            auth = validated(auth_path(root).read_text())
            gh("secret", "set", SECRET, "--repo", REPO, "--env", ENVIRONMENT, data=json.dumps(auth))
            gh("variable", "set", STATE, "--repo", REPO, "--env", ENVIRONMENT,
               "--body", json.dumps({"state": "ready", "generation": digest(auth)}))
            from scout_v2.state_store import atomic_json_write
            atomic_json_write(marker, {"mode": "github_secrets", "repository": REPO, "environment": ENVIRONMENT})
            # Old controller builds now fail auth_ready too, until replacement.
            # A disabled private backup is removed after the hosted smoke passes.
            auth_path(root).rename(auth_path(root).with_name("retired-auth.json"))
        print("Codex credentials migrated; legacy broker source retired", flush=True)
    finally:
        lock.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Codex migration stopped ({type(exc).__name__}); no credential data logged", file=sys.stderr)
        raise SystemExit(1)
