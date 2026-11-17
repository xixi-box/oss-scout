"""Run Codex CLI with private task credentials or legacy broker-owned refresh."""
from __future__ import annotations

import json
import hmac
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import re
import sys
import threading
from collections import deque
import os
import signal
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from .opencode_client import resolve_binary
from .reproduce import sanitized_env
from ..codex_auth import LEASE_TTL_SECONDS, valid_auth

CODEX_COMMUNICATION_RETRY_PREFIX = "Codex temporary communication failure:"


class CodexCommunicationError(RuntimeError):
    def __init__(self, detail, *, retryable=True):
        super().__init__(f"{CODEX_COMMUNICATION_RETRY_PREFIX} {detail}")
        self.retryable = retryable


class CodexClient:
    def __init__(self, *, timeout_seconds: int, workdir: Path, diagnostics_dir: Path | None = None,
                 read_only: bool = False):
        self.timeout = timeout_seconds
        self.workdir = workdir
        self.diagnostics_dir = diagnostics_dir
        self.read_only = read_only

    def _exchange(self, operation, *, request_timeout=None, **values):
        base = os.environ.get("DASHBOARD_URL", "").rstrip("/")
        token = os.environ.get("CF_INGEST_TOKEN", "")
        if not base.startswith("https://") or not token:
            raise RuntimeError("Codex credential broker is not configured")
        values.setdefault("request_id", uuid.uuid4().hex)
        request = urllib.request.Request(
            base + "/api/ingest/codex-auth", method="POST",
            data=json.dumps({"operation": operation, **values}).encode(),
            headers={"Content-Type": "application/json", "X-Ingest-Token": token,
                     "X-Request-ID": values["request_id"]})
        started = time.monotonic()
        try:
            timeout = request_timeout if request_timeout is not None else 15
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if operation == "acquire" and exc.code == 409:
                return {"busy": True}
            if exc.code == 503:
                try:
                    body = json.loads(exc.read(2048))
                except (OSError, ValueError):
                    body = {}
                if isinstance(body, dict) and body.get("retryable") is False:
                    raise RuntimeError("Codex broker requires explicit credential recovery or login") from None
            if exc.code in (408, 409, 429, 500, 502, 503, 504):
                raise CodexCommunicationError(
                    f"operation={operation}; HTTP {exc.code}; elapsed_seconds={time.monotonic()-started:.2f}",
                    retryable=exc.code != 409) from None
            raise RuntimeError(f"Codex login broker returned {exc.code}") from None
        except (OSError, ValueError) as exc:
            raise CodexCommunicationError(
                f"operation={operation}; kind={type(exc).__name__}; elapsed_seconds={time.monotonic()-started:.2f}") from None

    def _lease_request(self, operation, *, budget, attempts, record, **values):
        """Retry only idempotent lease operations; never replay an OAuth rotation."""
        if operation not in ("acquire", "release"):
            raise ValueError("Only acquisition and release may use lease retries")
        request_id = uuid.uuid4().hex
        deadline = time.monotonic() + max(0, budget)
        error = CodexCommunicationError(f"operation={operation}; request budget exhausted")
        for attempt in range(1, attempts + 1):
            sent_at = time.monotonic()
            remaining = deadline - sent_at
            if remaining <= 0:
                break
            try:
                response = self._exchange(operation, request_id=request_id,
                    request_timeout=min(45 if operation == "acquire" else 10, remaining), **values)
                if response.get("busy"):
                    raise CodexCommunicationError(f"operation={operation}; broker busy")
                if time.monotonic() >= deadline:
                    raise CodexCommunicationError(f"operation={operation}; request budget exhausted")
                record(f"auth.{operation}_ok", request_id=request_id, attempt=attempt,
                       request_seconds=round(time.monotonic() - sent_at, 3))
                return response, sent_at
            except CodexCommunicationError as exc:
                error = exc
                record(f"auth.{operation}_retry", request_id=request_id, attempt=attempt,
                       request_seconds=round(time.monotonic() - sent_at, 3),
                       retryable=exc.retryable)
                if not exc.retryable or attempt == attempts:
                    raise
                delay = min(2 ** attempt, max(0, deadline - time.monotonic()))
                if delay:
                    time.sleep(delay)
        raise error

    def refresh_credentials(self, credentials, recover=False):
        """Rotate once on the hosted runner, then persist using the broker's ticket.

        A failed OAuth response is never retried. A lost broker acknowledgement
        retries only the identical commit, which cannot rotate the upstream token.
        """
        from ..codex_auth import _refresh

        deadline = time.monotonic() + 35
        while True:
            grant = self._exchange("refresh_begin", protocol=2, lease=credentials["lease"],
                                   generation=credentials["generation"], recover=recover)
            if not grant.get("pending"):
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("Codex refresh owner is still pending; no upstream retry attempted")
            time.sleep(0.25)
        if not grant.get("ticket"):
            return grant
        try:
            updated = _refresh(grant["auth"])
        except (OSError, ValueError, RuntimeError) as exc:
            # Only class and HTTP status are safe to retain; exception strings and
            # response bodies may contain the rotating refresh token.
            failure = type(exc).__name__
            if isinstance(exc, urllib.error.HTTPError):
                failure += f" HTTP {exc.code}"
            try:
                self._exchange("refresh_abort", protocol=2, lease=credentials["lease"],
                               ticket=grant["ticket"], failure=failure)
            except (OSError, ValueError, RuntimeError):
                pass  # The pending ticket expires into quarantine if abort is lost.
            raise RuntimeError(f"Codex delegated token refresh failed ({failure}); login must be checked") from None
        for attempt in range(2):
            try:
                return self._exchange("refresh_commit", protocol=2, lease=credentials["lease"],
                                      ticket=grant["ticket"], auth=updated)
            except (OSError, ValueError, RuntimeError):
                if attempt:
                    raise RuntimeError("Codex refresh result awaits broker confirmation; no upstream retry attempted") from None
                time.sleep(0.2)

    def run(self, model: str, prompt: str) -> tuple[bool, str]:
        lease, process, reader = None, None, None
        local_auth = None
        refresh_proxy = None
        released, rc = False, None
        started = time.monotonic()
        tail = deque(maxlen=12)
        commands = {}
        last_error = ""
        secret_values = {str(v) for k, v in os.environ.items() if v and len(v) >= 6
                         and any(mark in k.upper() for mark in ("TOKEN", "SECRET", "KEY", "PASSWORD"))}
        baseline, subreaper = _prepare_process_cleanup()
        events = None
        if self.diagnostics_dir:
            self.diagnostics_dir.mkdir(parents=True, exist_ok=True)
            events = (self.diagnostics_dir / "codex-events.jsonl").open("a", encoding="utf-8")

        def secrets_from(value):
            if isinstance(value, dict):
                for nested in value.values():
                    secrets_from(nested)
            elif isinstance(value, str) and value:
                secret_values.add(value)

        def clean(text):
            for secret in sorted(secret_values, key=len, reverse=True):
                text = text.replace(secret, "[REDACTED]")
                text = text.replace(json.dumps(secret)[1:-1], "[REDACTED]")
            text = re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[REDACTED]", text)
            text = re.sub(r'(?i)("(?:access_token|refresh_token|id_token|api_key|authorization)"\s*:\s*)"[^"\n]*"',
                          r'\1"[REDACTED]"', text)
            text = re.sub(r"eyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[REDACTED]", text)
            text = re.sub(r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,})",
                          "[REDACTED]", text)
            return text

        def record(kind, **fields):
            row = {"timestamp": time.time(), "elapsed_seconds": round(time.monotonic() - started, 3),
                   "type": kind, **fields}
            if events:
                events.write(clean(json.dumps(row, ensure_ascii=False)) + "\n")
                events.flush()

        # The home contains credentials, and is never copied into artifacts.
        with tempfile.TemporaryDirectory(prefix="oss-codex-", dir=os.environ.get("RUNNER_TEMP"),
                                         ignore_cleanup_errors=True) as directory:
            home = Path(directory)
            home.chmod(0o700)
            auth = home / "auth.json"
            detail, ok = "Codex did not start", False
            try:
                if os.environ.get("OSSCOUT_CODEX_AUTH_MODE") == "github_secrets":
                    local_auth = _LocalAuth(self.workdir, self.diagnostics_dir)
                    credentials = {"auth": local_auth.value}
                else:
                    credentials, acquired_at = self._lease_request("acquire", protocol=2,
                        budget=min(90, self.timeout - (time.monotonic() - started)),
                        attempts=3, record=record)
                    lease = credentials["lease"]
                    # A slow response must not extend the server's lease deadline.
                    lease_valid_until = acquired_at + LEASE_TTL_SECONDS - 5
                secrets_from(credentials)
                auth.write_text(json.dumps(credentials["auth"]), encoding="utf-8")
                auth.chmod(0o600)
                if credentials.get("protocol") == 2:
                    refresh_proxy = _RefreshProxy(self, credentials, secrets_from)
                model, _, effort = model.partition("#")
                output = home / "last-message.txt"
                command = [resolve_binary("codex"), "exec", "--ignore-user-config", "--json",
                           "--ephemeral", "--sandbox", "read-only" if self.read_only else "danger-full-access",
                           "-c", 'cli_auth_credentials_store="file"',
                           "-c", 'approval_policy="never"',
                           "--color", "never", "-C", str(self.workdir), "-o", str(output)]
                if model:
                    command.extend(["-m", model])
                if effort:
                    command.extend(["-c", f'model_reasoning_effort="{effort}"'])
                command.append("-")
                env = sanitized_env()
                for key in ("OSSCOUT_CODEX_AUTH_MODE", "OSSCOUT_CODEX_AUTH_FILE",
                            "CODEX_REFRESH_TOKEN_URL_OVERRIDE"):
                    env.pop(key, None)
                env["CODEX_HOME"] = str(home)
                if refresh_proxy:
                    env["CODEX_REFRESH_TOKEN_URL_OVERRIDE"] = refresh_proxy.url
                    env["NO_PROXY"] = env.get("NO_PROXY", env.get("no_proxy", "")) + ",127.0.0.1,localhost"
                    env["no_proxy"] = env["NO_PROXY"]
                (home / "prompt.txt").write_text(prompt, encoding="utf-8")
                record("agent.started", model=model, reasoning_effort=effort, timeout_seconds=self.timeout)

                def capture():
                    nonlocal last_error
                    try:
                        for line in process.stdout:
                            # Tokens may have been refreshed since the last broker sync.
                            try:
                                secrets_from(_read_private_auth(auth) if local_auth else
                                             json.loads(auth.read_text(encoding="utf-8")))
                            except (OSError, ValueError, RuntimeError):
                                pass
                            line = clean(line.strip())
                            try:
                                event = json.loads(line)
                                if not isinstance(event, dict):
                                    raise ValueError("not an event object")
                            except ValueError:
                                event = {"type": "stderr", "message": line[-16000:]}
                            item = event.get("item") or {}
                            if item.get("type") == "reasoning":
                                continue
                            kind = event.get("type", "unknown")
                            item_id = str(item.get("id") or "")
                            if item.get("type") == "command_execution":
                                if kind == "item.started":
                                    commands[item_id] = time.monotonic()
                                elif item_id in commands:
                                    event["command_elapsed_seconds"] = round(time.monotonic() - commands.pop(item_id), 3)
                                print("[codex] " + clean(json.dumps({
                                    "event": kind, "elapsed_seconds": round(time.monotonic() - started, 2),
                                    "command": item.get("command"), "exit_code": item.get("exit_code"),
                                    "command_elapsed_seconds": event.get("command_elapsed_seconds"),
                                }, ensure_ascii=False)), flush=True)
                            if kind in ("error", "turn.failed"):
                                last_error = str(event.get("message") or event.get("error") or event)
                            record("agent.event", event=event)
                            tail.append(line[-1500:])
                    except (OSError, ValueError) as exc:
                        record("log.capture_error", error=type(exc).__name__)

                with (home / "prompt.txt").open("r", encoding="utf-8") as source:
                    process = subprocess.Popen(
                        command, cwd=self.workdir, env=env, stdin=source, stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                        bufsize=1, start_new_session=os.name != "nt")
                    if getattr(process, "stdout", None) is not None:
                        reader = threading.Thread(target=capture, daemon=True, name="codex-diagnostics")
                        reader.start()
                    while True:
                        try:
                            rc = process.wait(timeout=min(20, max(0.05, self.timeout - (time.monotonic() - started))))
                            break
                        except subprocess.TimeoutExpired:
                            elapsed = round(time.monotonic() - started, 2)
                            record("agent.heartbeat", elapsed=elapsed, active_commands=list(commands))
                            print(f"[codex] elapsed={elapsed}s; active_commands={list(commands)}", flush=True)
                            if elapsed >= self.timeout:
                                raise RuntimeError(f"Codex timed out after {self.timeout}s")
                            if local_auth:
                                continue  # Diagnostic heartbeat only; CLI refreshes its own file.
                            attempt = 0
                            while True:
                                now = time.monotonic()
                                if now >= started + self.timeout:
                                    raise RuntimeError(f"Codex timed out after {self.timeout}s")
                                if now >= lease_valid_until:
                                    raise CodexCommunicationError("operation=sync; lease renewal deadline exceeded")
                                if process.poll() is not None:
                                    break  # The CLI finished while the broker was unavailable.
                                attempt += 1
                                try:
                                    renewed_at = time.monotonic()
                                    self._exchange("sync", lease=lease, protocol=credentials.get("protocol", 1),
                                                   # A slow durable renewal can exceed 15s under
                                                   # controller I/O pressure. Wait for its receipt,
                                                   # always bounded by the existing lease deadline.
                                                   request_timeout=min(45, lease_valid_until-now,
                                                                       started+self.timeout-now),
                                                   **({} if refresh_proxy else {"auth": json.loads(auth.read_text())}))
                                    if time.monotonic() >= lease_valid_until:
                                        raise CodexCommunicationError("operation=sync; lease renewal deadline exceeded")
                                    lease_valid_until = renewed_at + LEASE_TTL_SECONDS - 5
                                    record("auth.heartbeat_ok", attempt=attempt,
                                           request_seconds=round(time.monotonic()-renewed_at, 3))
                                    break
                                except (CodexCommunicationError, TimeoutError, urllib.error.URLError) as exc:
                                    remaining = lease_valid_until - time.monotonic()
                                    record("auth.heartbeat_retry", operation="sync", attempt=attempt,
                                           error_type=type(exc).__name__,
                                           request_seconds=round(time.monotonic()-renewed_at, 3),
                                           lease_seconds_remaining=round(max(0, remaining), 3))
                                    print(f"[codex] heartbeat retry={attempt}; kind={type(exc).__name__}; "
                                          f"lease_seconds_remaining={max(0,remaining):.1f}", flush=True)
                                    if not getattr(exc, "retryable", True):
                                        raise
                                    if remaining <= 0:
                                        raise CodexCommunicationError("operation=sync; lease renewal deadline exceeded") from None
                                    time.sleep(min(10, 2 ** min(attempt, 3), remaining,
                                                   max(0, started+self.timeout-time.monotonic())))
                detail = output.read_text(encoding="utf-8") if output.exists() else ""
                ok = rc == 0 and bool(detail.strip())
                if rc:
                    detail = f"Codex exited with status {rc}"
                elif not detail.strip():
                    detail = "Codex returned no final message"
            except (OSError, ValueError, RuntimeError) as exc:
                detail = str(exc) if isinstance(exc, RuntimeError) else f"Codex start/result error: {type(exc).__name__}"
            finally:
                cleanup_errors = _stop_process_tree(process, baseline)
                if reader:
                    reader.join(timeout=5)
                    if not reader.is_alive():
                        process.stdout.close()
                if local_auth:
                    try:
                        latest_auth = _read_private_auth(auth)
                        secrets_from(latest_auth)
                        local_auth.persist(latest_auth)
                        record("auth.local_saved")
                    except (OSError, ValueError, RuntimeError):
                        local_auth.block()
                        ok, detail = False, "Codex local credential persistence failed; recovery required"
                        record("auth.local_save_failed")
                    finally:
                        local_auth.close()
                if lease and not released:
                    try:
                        latest_auth = json.loads(auth.read_text())
                        secrets_from(latest_auth)
                        self._lease_request("release", lease=lease,
                            protocol=credentials.get("protocol", 1), budget=25, attempts=2, record=record,
                            **({} if refresh_proxy else {"auth": latest_auth}))
                        released = True
                    except (OSError, ValueError, RuntimeError):
                        record("auth.release_failed")
                if refresh_proxy:
                    refresh_proxy.close()
                if not ok:
                    evidence = last_error or "\n".join(tail)[-1500:]
                    if evidence:
                        detail += "; last output: " + evidence
                detail = clean(detail)
                record("agent.finished", ok=ok, returncode=rc, detail=detail[:8000],
                       cleanup_errors=cleanup_errors)
                if events and (reader is None or not reader.is_alive()):
                    events.close()
                _restore_subreaper(subreaper)
        return ok, detail[:8000]


def _private_parent(path):
    if not path.is_absolute() or any(part.is_symlink() for part in (path, *path.parents)):
        raise RuntimeError("Codex local credentials require a private regular path")
    parent = path.parent.stat()
    if os.name != "nt" and (parent.st_mode & 0o077 or parent.st_uid != os.getuid()):
        raise RuntimeError("Codex local credential directory is not private")
    return parent.st_dev, parent.st_ino


def _read_private_auth(path):
    _private_parent(path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "r", encoding="utf-8") as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 1024 * 1024
                or (os.name != "nt" and (info.st_mode & 0o077 or info.st_uid != os.getuid()))):
            raise RuntimeError("Codex local credential file is not private")
        value = json.load(source)
    if (not valid_auth(value) or any(not isinstance(value["tokens"].get(key), str)
                                    for key in ("access_token", "refresh_token"))):
        raise RuntimeError("Codex local credential file is invalid")
    return value


class _LocalAuth:
    """Hold an OS lock through CLI execution; compare before replacing shared auth."""
    def __init__(self, workdir, diagnostics_dir):
        self.path = Path(os.environ.get("OSSCOUT_CODEX_AUTH_FILE", ""))
        self.parent_identity = _private_parent(self.path)
        if self.path.with_name("writeback-blocked").exists():
            raise RuntimeError("Codex local credentials require recovery before reuse")
        for forbidden in (workdir, diagnostics_dir):
            if forbidden and self.path.resolve().is_relative_to(Path(forbidden).resolve()):
                raise RuntimeError("Codex local credentials must be outside task and artifact directories")
        self.lock = None
        lock_path = self.path.with_name("auth.lock")
        _private_parent(lock_path)
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            self.lock = os.fdopen(descriptor, "r+b")
            info = os.fstat(self.lock.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise RuntimeError("Codex local credential lock is unsafe")
            if os.name == "nt":
                import msvcrt
                if info.st_size == 0:
                    self.lock.write(b"0")
                    self.lock.flush()
                self.lock.seek(0)
                msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.value = _read_private_auth(self.path)
            self.baseline = self._fingerprint(self.value)
        except (OSError, ValueError, RuntimeError):
            self.close()
            raise RuntimeError("Codex local credentials unavailable, invalid or busy") from None

    @staticmethod
    def _fingerprint(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).digest()

    def _write(self, path, value):
        if _private_parent(path) != self.parent_identity:
            raise RuntimeError("Codex local credential directory changed")
        descriptor, temporary = tempfile.mkstemp(prefix=".auth-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
                json.dump(value, destination)
                destination.flush()
                os.fsync(destination.fileno())
            if _private_parent(path) != self.parent_identity:
                raise RuntimeError("Codex local credential directory changed")
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def persist(self, value):
        if any(value["tokens"].get(key) != self.value["tokens"].get(key)
               for key in ("account_id", "login_owner_id")):
            raise RuntimeError("Codex local credential identity changed")
        if self._fingerprint(_read_private_auth(self.path)) != self.baseline:
            self._write(self.path.with_name("recovery-auth.json"), value)
            raise RuntimeError("Codex local credentials changed during execution")
        self._write(self.path, value)

    def block(self):
        try:
            self._write(self.path.with_name("writeback-blocked"), "Credential recovery required")
        except (OSError, ValueError, RuntimeError):
            pass  # Never follow an unsafe directory just to record a marker.

    def close(self):
        if self.lock:
            self.lock.close()
            self.lock = None


class _RefreshProxy:
    """A loopback-only OAuth adapter; only the parent process sees refresh authority.

    CLI 0.167.1 reads CODEX_REFRESH_TOKEN_URL_OVERRIDE and sends JSON OAuth requests.
    Each runner sees its own opaque refresh handle and never writes authority tokens.
    """
    def __init__(self, client, credentials, secrets_from):
        self.credentials = credentials
        self.handle = credentials["auth"]["tokens"]["refresh_token"]
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(5)

            def log_message(self, *args):
                pass  # Request data and credential handles must never enter logs.

            def do_POST(self):
                status, response = 400, {"error": "invalid_request"}
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 4096 or self.path != "/oauth/token":
                        raise ValueError()
                    body = json.loads(self.rfile.read(length))
                    if (body.get("grant_type") != "refresh_token" or not isinstance(body.get("refresh_token"), str)
                            or not hmac.compare_digest(body["refresh_token"], proxy.handle)):
                        status, response = 401, {"error": "invalid_grant"}
                    else:
                        fresh = client.refresh_credentials(proxy.credentials)
                        if fresh.get("protocol") != 2:
                            raise RuntimeError("Unsupported credential protocol")
                        secrets_from(fresh)
                        proxy.credentials = fresh
                        tokens = fresh["auth"]["tokens"]
                        response = {key: tokens[key] for key in ("access_token", "id_token") if tokens.get(key)}
                        response["refresh_token"] = proxy.handle
                        status = 200
                except (OSError, ValueError, RuntimeError):
                    status, response = 503, {"error": "temporarily_unavailable"}
                encoded = json.dumps(response).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                try:
                    self.wfile.write(encoded)
                except OSError:
                    pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True, name="codex-refresh-proxy")
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/oauth/token"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _linux_descendants():
    """Identify children by PID plus start time, including adopted daemon children."""
    if not sys.platform.startswith("linux"):
        return {}
    rows = {}
    for path in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = path.read_text().rsplit(")", 1)[1].split()
            rows[int(path.parent.name)] = (int(fields[1]), fields[19])
        except (OSError, ValueError, IndexError):
            continue
    owned, parents = {}, {os.getpid()}
    while True:
        children = {pid for pid, (parent, _) in rows.items() if parent in parents} - parents
        if not children:
            return owned
        owned.update({pid: rows[pid][1] for pid in children})
        parents.update(children)


def _prepare_process_cleanup():
    previous = None
    if sys.platform.startswith("linux"):
        import ctypes
        libc = ctypes.CDLL(None)
        value = ctypes.c_int()
        if libc.prctl(37, ctypes.byref(value), 0, 0, 0) == 0 and libc.prctl(36, 1, 0, 0, 0) == 0:
            previous = value.value
    return _linux_descendants(), previous


def _restore_subreaper(previous):
    if previous is not None:
        import ctypes
        ctypes.CDLL(None).prctl(36, previous, 0, 0, 0)


def _stop_process_tree(process, baseline):
    errors = []
    if process is None:
        return errors
    if os.name == "nt":
        if process.poll() is None:
            try:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               capture_output=True, timeout=10)
                process.kill()
            except (OSError, subprocess.TimeoutExpired):
                errors.append("Windows process-tree cleanup failed")
    else:
        # Kill the session even if its leader already exited. Linux subreaping
        # also catches build daemons that created their own session.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            errors.append("process-group cleanup failed")
        for _ in range(3):
            children = _linux_descendants()
            for pid, start in children.items():
                if baseline.get(pid) == start:
                    continue
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError:
                    errors.append(f"child cleanup failed: {pid}")
            for pid in children:
                if pid not in baseline and pid != process.pid:
                    try:
                        os.waitpid(pid, os.WNOHANG)
                    except ChildProcessError:
                        pass
            if not any(pid not in baseline for pid in _linux_descendants()):
                break
            time.sleep(0.05)
    try:
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        errors.append("CLI did not exit after cleanup")
    return errors
