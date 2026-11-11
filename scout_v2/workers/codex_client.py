"""Run Codex CLI with an exclusive, renewable ChatGPT subscription login."""
from __future__ import annotations

import json
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
    def __init__(self, *, timeout_seconds: int, workdir: Path):
        self.timeout = timeout_seconds
        self.workdir = workdir

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
            with urllib.request.urlopen(request, timeout=15) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if operation == "acquire" and exc.code == 409:
                return {"busy": True}
            raise RuntimeError(f"Codex login broker returned {exc.code}") from None

    def run(self, model: str, prompt: str) -> tuple[bool, str]:
        lease = None
        process = None
        released = False
        with tempfile.TemporaryDirectory(prefix="oss-codex-", dir=os.environ.get("RUNNER_TEMP")) as directory:
            home = Path(directory)
            auth = home / "auth.json"
            try:
                credentials = self._exchange("acquire")
                started = time.monotonic()
                while credentials.get("busy"):
                    if time.monotonic() - started >= self.timeout:
                        return False, "Codex login is busy; timed out waiting for the active task"
                    time.sleep(10)
                    credentials = self._exchange("acquire")
                lease = credentials["lease"]
                auth.write_text(json.dumps(credentials["auth"]), encoding="utf-8")
                auth.chmod(0o600)
                model, _, effort = model.partition("#")
                output = home / "last-message.txt"
                command = [resolve_binary("codex"), "exec", "--ignore-user-config",
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
                # Keep the prompt and output outside the target repository.
                (home / "prompt.txt").write_text(prompt, encoding="utf-8")
                with (home / "prompt.txt").open("r", encoding="utf-8") as source, (
                    home / "execution.log").open("w", encoding="utf-8") as log:
                    process = subprocess.Popen(
                        command, cwd=self.workdir, env=env, stdin=source, stdout=log,
                        stderr=log, start_new_session=os.name != "nt")
                    while True:
                        try:
                            rc = process.wait(timeout=20)
                            break
                        except subprocess.TimeoutExpired:
                            if time.monotonic() - started >= self.timeout:
                                raise RuntimeError(f"Codex timed out after {self.timeout}s")
                            self._exchange("sync", lease=lease, auth=json.loads(auth.read_text()))
                self._exchange("release", lease=lease, auth=json.loads(auth.read_text()))
                released = True
                # Only the final message is returned; session/debug logs can contain credentials.
                detail = output.read_text(encoding="utf-8") if output.exists() else ""
                if rc:
                    return False, f"Codex exited with status {rc}"
                return (True, detail[:8000]) if detail.strip() else (False, "Codex returned no final message")
            except (OSError, ValueError, RuntimeError) as exc:
                return False, str(exc) if isinstance(exc, RuntimeError) else "Codex could not start or load its result"
            finally:
                if process is not None and process.poll() is None:
                    if os.name == "nt":
                        process.kill()
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                if lease and not released:
                    try:
                        self._exchange("release", lease=lease, auth=json.loads(auth.read_text()))
                    except (OSError, ValueError, RuntimeError):
                        pass
