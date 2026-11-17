"""ChatGPT credentials: concurrent execution and a single delegated refresh ticket."""
from __future__ import annotations

from contextlib import contextmanager
import copy
import datetime as dt
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from .state_store import atomic_json_write

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
TOKEN_URL = "https://auth.openai.com/oauth/token"
LEASE_TTL_SECONDS = 120


@contextmanager
def _broker_phase(operation: str, phase: str, request_id: str = "-"):
    """Only fixed phase/operation names are logged; never request data or tokens."""
    started = time.monotonic()
    try:
        yield
    finally:
        elapsed = time.monotonic() - started
        if elapsed >= 1:
            logging.getLogger(__name__).warning(
                "Codex broker slow operation=%s phase=%s elapsed_seconds=%.3f request_id=%s",
                operation, phase, elapsed, request_id)


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
def _broker_lock(root: Path, operation: str = "administration", request_id: str = "-"):
    # OAuth can take seconds. Never hold the queue/dispatcher locks over network I/O.
    directory = auth_path(root).parent
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    path = directory / "broker-lock.sqlite3"
    connection = sqlite3.connect(path, timeout=5, isolation_level=None)
    path.chmod(0o600)
    try:
        with _broker_phase(operation, "lock_wait", request_id):
            connection.execute("BEGIN IMMEDIATE")
        with _broker_phase(operation, "lock_hold", request_id):
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
        (auth_path(root).parent / "refresh-ticket.json").unlink(missing_ok=True)


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
    """Protocol 2 shares access tokens and delegates one refresh to an authenticated runner.

    The old exclusive protocol remains valid for already executing old workers.
    """
    operation = body.get("operation")
    directory = auth_path(root).parent
    lease_path = directory / "lease.json"
    leases_path = directory / "leases.json"
    operation_label = operation if isinstance(operation, str) and operation in {
        "acquire", "sync", "release", "refresh", "refresh_begin", "refresh_commit", "refresh_abort"
    } else "invalid"
    request_id = body.get("request_id")
    if request_id is not None and (not isinstance(request_id, str) or not re.fullmatch(r"[0-9a-f]{32}", request_id)):
        return {"detail": "Invalid broker request ID"}, 400
    with _broker_phase(operation_label, "total", request_id or "-"), _broker_lock(root, operation_label, request_id or "-"):
        with _broker_phase(operation_label, "read_auth", request_id or "-"):
            value = _load(auth_path(root))
        if not valid_auth(value):
            return {"detail": "Codex 尚未登录 ChatGPT"}, 401
        now = time.time()
        with _broker_phase(operation_label, "read_leases", request_id or "-"):
            leases = _active_leases(root, now)
        lease_id = body.get("lease")
        if lease_id is not None and not isinstance(lease_id, str):
            return {"detail": "Invalid Codex login lease"}, 400
        if operation == "sync" and body.get("protocol") == 2:
            # Heartbeats need the current identity and unexpired execution lease,
            # not the OAuth ticket/quarantine files or legacy worker state. A
            # heartbeat cannot rotate tokens or clear a refresh quarantine.
            lease = leases.get(lease_id)
            if (not lease or lease.get("expires", 0) <= time.time()
                    or lease.get("identity") != list(_identity(value))):
                return {"detail": "Codex login lease expired or identity changed"}, 409
            lease["expires"] = time.time() + LEASE_TTL_SECONDS
            with _broker_phase("sync", "lease_persist", request_id or "-"):
                # Atomic fsync and readback remain; an old lease backup is never
                # a valid recovery source and adds avoidable reads/writes.
                atomic_json_write(leases_path, leases, backup=False)
            return {"ok": True}, 200
        legacy = _load(lease_path)
        concurrent = body.get("protocol") == 2 or lease_id in leases
        if concurrent:
            generation = _generation(value)
            blocked_path = directory / "refresh-blocked.json"
            ticket_path = directory / "refresh-ticket.json"
            blocked = _load(blocked_path)
            ticket = _load(ticket_path)
            ticket_pending = ticket.get("state") == "pending" and ticket.get("generation") == generation
            if ticket_pending and ticket.get("expires", 0) <= now:
                blocked = {**(blocked if blocked.get("generation") == generation else {}),
                           "generation": generation, "reason": "refresh outcome uncertain"}
                atomic_json_write(blocked_path, blocked)
            quarantined = blocked.get("generation") == generation
            recovering = body.get("recover") is True
            if operation == "acquire":
                if legacy.get("expires", 0) > now:
                    return {"detail": "Codex legacy task is still using credentials"}, 409
                if not value["tokens"].get("account_id"):
                    return {"detail": "Codex account identity is missing; log in again"}, 401
                if quarantined and not (recovering and (not blocked.get("recovery_attempted")
                        or (ticket_pending and ticket.get("expires", 0) > now))):
                    return {"detail": "Codex refresh outcome is uncertain; explicit recovery or login required", "retryable": False}, 503
                prior = next(((key, lease) for key, lease in leases.items()
                              if request_id and lease.get("request_id") == request_id), None)
                if prior:
                    lease_id, lease = prior
                    if lease.get("identity") != list(_identity(value)):
                        return {"detail": "Codex acquisition identity changed"}, 409
                    lease["expires"] = now + LEASE_TTL_SECONDS
                else:
                    lease_id = uuid.uuid4().hex
                    leases[lease_id] = {"expires": now + LEASE_TTL_SECONDS, "identity": list(_identity(value)),
                                        "recover": recovering}
                    if request_id:
                        leases[lease_id]["request_id"] = request_id
            elif operation in ("sync", "release", "refresh", "refresh_begin", "refresh_commit", "refresh_abort"):
                if operation == "release" and lease_id and lease_id not in leases:
                    return {"ok": True}, 200
                lease = leases.get(lease_id)
                owns_ticket = (ticket.get("id") and ticket.get("id") == body.get("ticket")
                               and ticket.get("lease") == lease_id
                               and ticket.get("identity") == list(_identity(value)))
                # A lost commit response can be retried with the same ticket, even after
                # the heartbeat expired. No successor refresh is allowed to overwrite it.
                ticket_commit = operation == "refresh_commit" and owns_ticket
                if not ticket_commit and (not lease or lease.get("identity") != list(_identity(value))):
                    return {"detail": "Codex login lease expired or identity changed"}, 409
                if operation == "refresh_commit":
                    returned = body.get("auth")
                    if not owns_ticket or not valid_auth(returned) or _identity(returned) != _identity(value):
                        return {"detail": "Invalid Codex refresh ticket or identity"}, 409
                    returned_generation = _generation(returned)
                    if ticket.get("state") == "committed":
                        if ticket.get("result_generation") != generation or returned_generation != generation:
                            return {"detail": "Codex refresh ticket was superseded"}, 409
                    elif ticket.get("generation") != generation:
                        # The credential write may have succeeded before the receipt write failed.
                        if returned_generation != generation:
                            return {"detail": "Codex refresh generation changed"}, 409
                    elif ticket.get("state") != "pending":
                        return {"detail": "Codex refresh ticket is no longer valid"}, 409
                    else:
                        _save_auth(root, returned)
                        value = returned
                        generation = returned_generation
                    ticket.update(state="committed", result_generation=generation)
                    atomic_json_write(ticket_path, ticket)
                    blocked_path.unlink(missing_ok=True)
                    leases[lease_id] = {"expires": now + LEASE_TTL_SECONDS, "identity": list(_identity(value))}
                elif operation == "refresh_abort":
                    if not owns_ticket or ticket.get("state") != "pending" or ticket.get("generation") != generation:
                        return {"detail": "Codex refresh ticket is no longer valid"}, 409
                    ticket["state"] = "aborted"
                    atomic_json_write(ticket_path, ticket)
                    failure = body.get("failure", "")
                    allowed_failures = {"URLError", "TimeoutError", "OSError", "ValueError", "RuntimeError"}
                    if failure not in allowed_failures and not (
                            isinstance(failure, str) and re.fullmatch(r"HTTPError HTTP [1-5][0-9]{2}", failure)):
                        failure = "unknown"
                    atomic_json_write(blocked_path, {**(blocked if quarantined else {}),
                        "generation": generation, "reason": "delegated refresh failed", "failure": failure})
                elif operation in ("refresh", "refresh_begin"):
                    if body.get("generation") != generation:
                        pass  # Another owner already refreshed; return its authoritative snapshot.
                    elif operation == "refresh":
                        # Compatibility with already executing older protocol-2 workers:
                        # never make a VPS OAuth request or leak an unexecuted ticket to them.
                        return {"detail": "Codex refresh requires the delegated runner protocol", "retryable": False}, 503
                    elif ticket_pending and ticket.get("expires", 0) > now:
                        leases[lease_id]["expires"] = now + LEASE_TTL_SECONDS
                        atomic_json_write(leases_path, leases)
                        return {"protocol": 2, "pending": True, "retry_after": 0.25}, 200
                    elif quarantined and not (recovering and lease.get("recover")
                                               and not blocked.get("recovery_attempted")):
                        return {"detail": "Codex refresh outcome is uncertain; explicit recovery or login required", "retryable": False}, 503
                    else:
                        ticket = {"id": uuid.uuid4().hex, "lease": lease_id,
                                  "identity": list(_identity(value)), "generation": generation,
                                  "state": "pending", "expires": now + LEASE_TTL_SECONDS}
                        atomic_json_write(ticket_path, ticket)
                        if quarantined:
                            atomic_json_write(blocked_path, {**blocked, "recovery_attempted": True})
                        leases[lease_id]["expires"] = now + LEASE_TTL_SECONDS
                        atomic_json_write(leases_path, leases)
                        # Only this authenticated ticket holder gets the real refresh token.
                        # It remains in the runner's parent Python process, never CLI auth.json.
                        return {"protocol": 2, "lease": lease_id, "generation": generation,
                                "ticket": ticket["id"], "auth": value}, 200
                if operation == "release":
                    del leases[lease_id]
                elif operation != "refresh_commit":
                    leases[lease_id]["expires"] = now + LEASE_TTL_SECONDS
            else:
                return {"detail": "Invalid Codex credential operation"}, 400
            with _broker_phase(operation_label, "lease_persist", request_id or "-"):
                atomic_json_write(leases_path, leases, backup=False)
            if operation in ("acquire", "refresh", "refresh_begin", "refresh_commit"):
                return {"protocol": 2, "lease": lease_id, "generation": _generation(value),
                        "auth": _snapshot(value, lease_id)}, 200
            return {"ok": True}, 200

        if operation == "acquire":
            if _load(directory / "refresh-blocked.json").get("generation") == _generation(value):
                return {"detail": "Codex refresh outcome is uncertain; log in again", "retryable": False}, 503
            if legacy.get("expires", 0) > now or leases:
                return {"detail": "Codex 正在被另一任务使用"}, 409
            legacy = {"id": uuid.uuid4().hex, "expires": now + LEASE_TTL_SECONDS}
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
            legacy["expires"] = now + LEASE_TTL_SECONDS
            atomic_json_write(lease_path, legacy)
        return {"ok": True}, 200


if __name__ == "__main__":
    import sys
    root = Path(os.environ.get("OSSCOUT_DASHBOARD_ROOT", "/data"))
    install_auth(root, json.load(sys.stdin))
    print("Codex subscription login installed")
