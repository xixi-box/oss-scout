"""Trusted Actions steps only: serialize, restore and persist subscription auth.

No credential broker and no OAuth calls here. The CLI owns token refresh. A live
environment variable guards the encrypted secret against stale reads/write loss.
Never print subprocess output: GitHub errors can contain sensitive request data.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
import uuid

REPO = "xixi-box/oss-scout"
ENVIRONMENT = "codex-auth"
SECRET = "CODEX_AUTH_JSON"
STATE = "CODEX_AUTH_STATE"
API = f"repos/{REPO}/environments/{ENVIRONMENT}/variables/{STATE}"


def gh(*args, data=None):
    # This process alone gets the PAT. It must never run inside the model CLI.
    for attempt in range(3):
        try:
            result = subprocess.run(["gh", *args], input=data, text=True,
                                    capture_output=True, timeout=40)
            if result.returncode == 0:
                return result.stdout
        except (OSError, subprocess.TimeoutExpired):
            pass
        if attempt < 2:
            time.sleep(2 ** attempt)
    raise RuntimeError("GitHub credential storage request failed; subsequent tasks remain blocked")


def read_state():
    return json.loads(json.loads(gh("api", API))["value"])


def set_state(value):
    gh("api", API, "--method", "PATCH", "--input", "-",
       data=json.dumps({"name": STATE, "value": json.dumps(value, sort_keys=True)}))


def validated(raw):
    value = json.loads(raw)
    tokens = value.get("tokens", {})
    if (value.get("OPENAI_API_KEY") or not isinstance(tokens, dict)
            or any(not isinstance(tokens.get(k), str) or not tokens[k]
                   for k in ("access_token", "refresh_token", "account_id"))
            or tokens["refresh_token"].startswith("oss-scout-lease:")):
        raise ValueError("Expected full ChatGPT subscription credentials")
    return value


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def identity(value):
    tokens = value["tokens"]
    return hashlib.sha256(json.dumps([tokens.get("account_id"), tokens.get("login_owner_id")]).encode()).hexdigest()


def private_read(path):
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            raise ValueError("Refusing symlink in credential storage")
    if os.name != "nt" and stat.S_IMODE(path.parent.stat().st_mode) & 0o077:
        raise ValueError("Credential directory must be private")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "r", encoding="utf-8") as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 1024 * 1024
                or (os.name != "nt" and (stat.S_IMODE(info.st_mode) & 0o077 or info.st_uid != os.getuid()))):
            raise ValueError("Credential file must be private")
        return source.read()


def write_private(path, value):
    with open(path, "x", encoding="utf-8", opener=lambda p, f: os.open(p, f, 0o600)) as handle:
        json.dump(value, handle)


def directory():
    return Path(os.environ["RUNNER_TEMP"]).resolve() / "oss-codex-auth"


def restore():
    if os.environ.get("GITHUB_REF") != "refs/heads/main":
        raise ValueError("Subscription credentials are restricted to main")
    value = validated(os.environ.pop(SECRET))
    state = read_state()  # Live read after the concurrency lock, never vars context.
    if state.get("state") != "ready" or state.get("generation") != digest(value):
        raise ValueError("Codex session unavailable or secret snapshot stale; refusing fallback")
    folder = directory()
    folder.mkdir(mode=0o700)
    session = {"owner": os.environ["GITHUB_RUN_ID"] + ":" + os.environ.get("GITHUB_RUN_ATTEMPT", "1"),
               "nonce": uuid.uuid4().hex, "identity": identity(value), "generation": digest(value)}
    write_private(folder / "auth.json", value)
    write_private(folder / "session.json", session)
    set_state({**session, "state": "in_use"})
    # Only a positively acknowledged guard permits CLI execution.
    write_private(folder / "armed.json", {"armed": True})
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write("ready=true\n")
    print("Codex credentials restored; exclusive session armed")


def persist():
    folder = directory()
    session = json.loads(private_read(folder / "session.json"))
    private_read(folder / "armed.json")
    owner = os.environ["GITHUB_RUN_ID"] + ":" + os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    if session["owner"] != owner or (folder / "writeback-blocked").exists():
        raise ValueError("Codex credential recovery required; subsequent tasks remain blocked")
    state = read_state()
    if state.get("state") != "in_use" or any(state.get(k) != session[k] for k in ("owner", "nonce", "generation", "identity")):
        raise ValueError("Codex session ownership changed; refusing stale writeback")
    value = validated(private_read(folder / "auth.json"))
    if identity(value) != state["identity"]:
        raise ValueError("Codex account changed; refusing writeback")
    generation = digest(value)
    if generation != session["generation"]:
        gh("secret", "set", SECRET, "--repo", REPO, "--env", ENVIRONMENT, data=json.dumps(value))
    set_state({"state": "ready", "generation": generation})
    print("Codex credential writeback acknowledged; session released")


def cleanup():
    folder = directory()
    for path in (folder, *folder.parents):
        if path.is_symlink():
            raise ValueError("Refusing unsafe credential cleanup")
    if not folder.exists():
        return
    # Fixed private files only; never follow a model-created directory tree.
    for name in ("auth.json", "session.json", "armed.json", "auth.lock", "recovery-auth.json", "writeback-blocked"):
        (folder / name).unlink(missing_ok=True)
    folder.rmdir()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("restore", "persist", "cleanup"))
    args = parser.parse_args()
    try:
        {"restore": restore, "persist": persist, "cleanup": cleanup}[args.operation]()
    except Exception as exc:
        # No raw exception messages, JSON, subprocess output or paths.
        print(f"Codex Secrets {args.operation} failed ({type(exc).__name__}); session remains fail-closed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
