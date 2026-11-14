"""Run Codex CLI with concurrent ChatGPT access and broker-owned refresh."""
from __future__ import annotations

import json
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import re
import sys
import threading
from collections import deque
import os
import signal
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from .opencode_client import resolve_binary
from .reproduce import sanitized_env


class CodexClient:
    def __init__(self, *, timeout_seconds: int, workdir: Path, diagnostics_dir: Path | None = None):
        self.timeout = timeout_seconds
        self.workdir = workdir
        self.diagnostics_dir = diagnostics_dir

    def _exchange(self, operation, **values):
        base = os.environ.get("DASHBOARD_URL", "").rstrip("/")
        token = os.environ.get("CF_INGEST_TOKEN", "")
        if not base.startswith("https://") or not token:
            raise RuntimeError("Codex credential broker is not configured")
        request = urllib.request.Request(
            base + "/api/ingest/codex-auth", method="POST",
            data=json.dumps({"operation": operation, **values}).encode(),
            headers={"Content-Type": "application/json", "X-Ingest-Token": token})
        try:
            with urllib.request.urlopen(request, timeout=30 if operation == "refresh" else 15) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if operation == "acquire" and exc.code == 409:
                return {"busy": True}
            raise RuntimeError(f"Codex login broker returned {exc.code}") from None

    def run(self, model: str, prompt: str) -> tuple[bool, str]:
        lease, process, reader = None, None, None
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
            auth = home / "auth.json"
            detail, ok = "Codex did not start", False
            try:
                credentials = self._exchange("acquire", protocol=2)
                while credentials.get("busy"):
                    if time.monotonic() - started >= self.timeout:
                        raise RuntimeError("Codex login is busy; timed out waiting for the active task")
                    time.sleep(10)
                    credentials = self._exchange("acquire", protocol=2)
                lease = credentials["lease"]
                secrets_from(credentials)
                auth.write_text(json.dumps(credentials["auth"]), encoding="utf-8")
                auth.chmod(0o600)
                if credentials.get("protocol") == 2:
                    refresh_proxy = _RefreshProxy(self, credentials, secrets_from)
                model, _, effort = model.partition("#")
                output = home / "last-message.txt"
                command = [resolve_binary("codex"), "exec", "--ignore-user-config", "--json",
                           "--ephemeral", "--sandbox", "danger-full-access",
                           "-c", 'cli_auth_credentials_store="file"',
                           "-c", 'approval_policy="never"',
                           "--color", "never", "-C", str(self.workdir), "-o", str(output)]
                if model:
                    command.extend(["-m", model])
                if effort:
                    command.extend(["-c", f'model_reasoning_effort="{effort}"'])
                command.append("-")
                env = sanitized_env()
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
                                secrets_from(json.loads(auth.read_text(encoding="utf-8")))
                            except (OSError, ValueError):
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
                            self._exchange("sync", lease=lease, protocol=credentials.get("protocol", 1),
                                           **({} if refresh_proxy else {"auth": json.loads(auth.read_text())}))
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
                if lease and not released:
                    try:
                        latest_auth = json.loads(auth.read_text())
                        secrets_from(latest_auth)
                        self._exchange("release", lease=lease, protocol=credentials.get("protocol", 1),
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


class _RefreshProxy:
    """A loopback-only OAuth adapter; the real refresh token stays on the server.

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
                        fresh = client._exchange("refresh", protocol=2, lease=credentials["lease"],
                                                 generation=proxy.credentials["generation"])
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
