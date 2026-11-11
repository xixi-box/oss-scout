"""Persist ChatGPT CLI credentials and serialize their refresh across runners."""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

from .state_store import atomic_json_write, queue_transaction


def auth_path(root: Path) -> Path:
    return root / "state/codex/auth.json"


def valid_auth(value) -> bool:
    tokens = value.get("tokens") if isinstance(value, dict) else None
    return (isinstance(tokens, dict) and bool(tokens.get("access_token"))
            and bool(tokens.get("refresh_token")) and not value.get("OPENAI_API_KEY"))


def auth_ready(root: Path) -> bool:
    try:
        return valid_auth(json.loads(auth_path(root).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return False


def install_auth(root: Path, value: dict) -> None:
    if not valid_auth(value):
        raise ValueError("Expected ChatGPT subscription login credentials")
    path = auth_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    atomic_json_write(path, value)
    path.chmod(0o600)


def exchange(root: Path, body: dict) -> tuple[dict, int]:
    """The ingest-token-authenticated caller owns a renewable, exclusive lease."""
    operation = body.get("operation")
    lease_path = root / "state/codex/lease.json"
    now = time.time()
    with queue_transaction(root):
        if not auth_ready(root):
            return {"detail": "Codex 尚未登录 ChatGPT"}, 401
        lease = json.loads(lease_path.read_text()) if lease_path.exists() else {}
        if operation == "acquire":
            if lease.get("expires", 0) > now:
                return {"detail": "Codex 正在被另一任务使用"}, 409
            lease = {"id": uuid.uuid4().hex, "expires": now + 120}
            atomic_json_write(lease_path, lease)
            return {"lease": lease["id"], "auth": json.loads(auth_path(root).read_text())}, 200
        if (operation not in ("sync", "release") or not body.get("lease")
                or body["lease"] != lease.get("id") or lease.get("expires", 0) <= now):
            return {"detail": "Codex login lease expired"}, 409
        if body.get("auth") is not None:
            if not valid_auth(body["auth"]):
                return {"detail": "Invalid subscription credentials"}, 400
            install_auth(root, body["auth"])
        if operation == "release":
            lease_path.unlink(missing_ok=True)
        else:
            lease["expires"] = now + 120
            atomic_json_write(lease_path, lease)
        return {"ok": True}, 200


if __name__ == "__main__":
    # Used by the trusted self-hosted credential installation workflow.
    import sys

    root = Path(os.environ.get("OSSCOUT_DASHBOARD_ROOT", "/data"))
    with queue_transaction(root):
        lease_path = root / "state/codex/lease.json"
        lease = json.loads(lease_path.read_text()) if lease_path.exists() else {}
        if lease.get("expires", 0) > time.time():
            raise SystemExit("Codex is busy; install credentials after the task ends")
        install_auth(root, json.load(sys.stdin))
    print("Codex subscription login installed")
