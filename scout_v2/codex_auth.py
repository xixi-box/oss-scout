"""ChatGPT credentials: concurrent execution, one authoritative refresh owner."""
from __future__ import annotations

from contextlib import contextmanager
import copy
import datetime as dt
import hashlib
import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from .state_store import atomic_json_write

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
TOKEN_URL = "https://auth.openai.com/oauth/token"


def auth_path(root: Path) -> Path:
    return root / "state/codex/auth.json"


def valid_auth(value) -> bool:
    tokens = value.get("tokens") if isinstance(value, dict) else None
    return (isinstance(tokens, dict) and bool(tokens.get("access_token"))
            and bool(tokens.get("refresh_token")) and not value.get("OPENAI_API_KEY"))


def _load(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {} if default is None else default


def _generation(value: dict) -> str:
    return hashlib.sha256(json.dumps({"tokens": value.get("tokens"),
                                      "last_refresh": value.get("last_refresh")}, sort_keys=True).encode()).hexdigest()


def _identity(value: dict) -> tuple:
    tokens = value["tokens"]
    return tokens.get("account_id"), tokens.get("login_owner_id")


@contextmanager
def _broker_lock(root: Path):
    # OAuth can take seconds. Never hold the queue/dispatcher locks over network I/O.
    directory = auth_path(root).parent
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    path = directory / "broker-lock.sqlite3"
    connection = sqlite3.connect(path, timeout=30, isolation_level=None)
    path.chmod(0o600)
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


def auth_ready(root: Path) -> bool:
    try:
        value = _load(auth_path(root))
        blocked = _load(auth_path(root).parent / "refresh-blocked.json")
        return valid_auth(value) and blocked.get("generation") != _generation(value)
    except (OSError, ValueError):
        return False


def _save_auth(root: Path, value: dict) -> None:
    path = auth_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    atomic_json_write(path, value)
    path.chmod(0o600)


def _active_leases(root: Path, now: float) -> dict:
    return {key: lease for key, lease in _load(auth_path(root).parent / "leases.json").items()
            if lease.get("expires", 0) > now}


def install_auth(root: Path, value: dict) -> None:
    if not valid_auth(value):
        raise ValueError("Expected ChatGPT subscription login credentials")
    with _broker_lock(root):
        legacy = _load(auth_path(root).parent / "lease.json")
        if legacy.get("expires", 0) > time.time() or _active_leases(root, time.time()):
            raise RuntimeError("Codex is busy; install credentials after the tasks end")
        _save_auth(root, value)
        (auth_path(root).parent / "refresh-blocked.json").unlink(missing_ok=True)


def _snapshot(value: dict, lease: str) -> dict:
    snapshot = copy.deepcopy(value)
    # The CLI receives only an opaque broker handle, never the rotating refresh token.
    snapshot["tokens"]["refresh_token"] = "oss-scout-lease:" + lease
    return snapshot


def _refresh(value: dict) -> dict:
    provenance = value["tokens"].get("oauth_provenance") or {}
    if provenance.get("issuer", "https://auth.openai.com").rstrip("/") != "https://auth.openai.com":
        raise RuntimeError("Unsupported Codex OAuth authority")
    request = urllib.request.Request(TOKEN_URL, method="POST", headers={"Content-Type": "application/json"},
        data=json.dumps({"grant_type": "refresh_token", "client_id": provenance.get("client_id") or CLIENT_ID,
                         "refresh_token": value["tokens"]["refresh_token"]}).encode())
    with urllib.request.urlopen(request, timeout=10) as response:
        refreshed = json.load(response)
    if not isinstance(refreshed, dict) or not refreshed.get("access_token"):
        raise RuntimeError("Codex refresh returned incomplete credentials")
    result = copy.deepcopy(value)
    for key in ("access_token", "refresh_token", "id_token"):
        if refreshed.get(key):
            result["tokens"][key] = refreshed[key]
    result["last_refresh"] = dt.datetime.now(dt.timezone.utc).isoformat()
    return result


def exchange(root: Path, body: dict) -> tuple[dict, int]:
    """Protocol 2 shares access tokens, serializes refresh and forbids runner writes.

    The old exclusive protocol remains valid for already executing old workers.
    """
    operation = body.get("operation")
    directory = auth_path(root).parent
    lease_path = directory / "lease.json"
    leases_path = directory / "leases.json"
    with _broker_lock(root):
        value = _load(auth_path(root))
        if not valid_auth(value):
            return {"detail": "Codex 尚未登录 ChatGPT"}, 401
        now = time.time()
        legacy = _load(lease_path)
        leases = _active_leases(root, now)
        lease_id = body.get("lease")
        if lease_id is not None and not isinstance(lease_id, str):
            return {"detail": "Invalid Codex login lease"}, 400
        concurrent = body.get("protocol") == 2 or lease_id in leases
        if concurrent:
            if operation == "acquire":
                if legacy.get("expires", 0) > now:
                    return {"detail": "Codex legacy task is still using credentials"}, 409
                if not value["tokens"].get("account_id"):
                    return {"detail": "Codex account identity is missing; log in again"}, 401
                if _load(directory / "refresh-blocked.json").get("generation") == _generation(value):
                    return {"detail": "Codex refresh outcome is uncertain; log in again"}, 503
                lease_id = uuid.uuid4().hex
                leases[lease_id] = {"expires": now + 120, "identity": list(_identity(value))}
            elif operation in ("sync", "release", "refresh"):
                lease = leases.get(lease_id)
                if not lease or lease.get("identity") != list(_identity(value)):
                    return {"detail": "Codex login lease expired or identity changed"}, 409
                if operation == "refresh":
                    # A stale caller gets the already refreshed generation; it never reuses a token.
                    if body.get("generation") == _generation(value):
                        if _load(directory / "refresh-blocked.json").get("generation") == _generation(value):
                            return {"detail": "Codex refresh outcome is uncertain; log in again"}, 503
                        try:
                            updated = _refresh(value)
                        except (OSError, ValueError, RuntimeError):
                            # A lost response may have consumed the token. Never blindly retry it.
                            atomic_json_write(directory / "refresh-blocked.json", {"generation": _generation(value)})
                            return {"detail": "Codex token refresh failed; login must be checked"}, 503
                        _save_auth(root, updated)
                        value = updated
                if operation == "release":
                    del leases[lease_id]
                else:
                    leases[lease_id]["expires"] = now + 120
            else:
                return {"detail": "Invalid Codex credential operation"}, 400
            atomic_json_write(leases_path, leases)
            if operation in ("acquire", "refresh"):
                return {"protocol": 2, "lease": lease_id, "generation": _generation(value),
                        "auth": _snapshot(value, lease_id)}, 200
            # Runner auth is deliberately ignored: old snapshots cannot replace server tokens.
            return {"ok": True}, 200

        if operation == "acquire":
            if _load(directory / "refresh-blocked.json").get("generation") == _generation(value):
                return {"detail": "Codex refresh outcome is uncertain; log in again"}, 503
            if legacy.get("expires", 0) > now or leases:
                return {"detail": "Codex 正在被另一任务使用"}, 409
            legacy = {"id": uuid.uuid4().hex, "expires": now + 120}
            atomic_json_write(lease_path, legacy)
            return {"lease": legacy["id"], "auth": value}, 200
        if (operation not in ("sync", "release") or not lease_id
                or lease_id != legacy.get("id") or legacy.get("expires", 0) <= now):
            return {"detail": "Codex login lease expired"}, 409
        if body.get("auth") is not None:
            if not valid_auth(body["auth"]):
                return {"detail": "Invalid subscription credentials"}, 400
            _save_auth(root, body["auth"])
        if operation == "release":
            lease_path.unlink(missing_ok=True)
        else:
            legacy["expires"] = now + 120
            atomic_json_write(lease_path, legacy)
        return {"ok": True}, 200


if __name__ == "__main__":
    import sys
    root = Path(os.environ.get("OSSCOUT_DASHBOARD_ROOT", "/data"))
    install_auth(root, json.load(sys.stdin))
    print("Codex subscription login installed")
