"""Two serial CLI calls using a single local subscription session; no PR writes."""
import os
import json
import base64
from pathlib import Path
import tempfile
import sys
import subprocess

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scout_v2.workers.codex_client import CodexClient


def main():
    if os.environ.get("OSSCOUT_CODEX_AUTH_MODE") != "github_secrets":
        raise RuntimeError("This check requires GitHub Secrets local mode")
    model, effort = os.environ.get("CHECK_MODEL", "gpt-6.1-sol#medium").split("#", 1)
    probe_access = None
    if os.environ.get("CHECK_REFRESH") == "true":
        # 0.167.1 prefers JWT exp over last_refresh. Expire the private copy so
        # native proactive refresh runs; never call OAuth ourselves. The refresh
        # authority stays valid, and no modified access token is saved as ready.
        auth = Path(os.environ["OSSCOUT_CODEX_AUTH_FILE"])
        value = json.loads(auth.read_text())
        access = value["tokens"]["access_token"]
        pieces = access.split(".")
        if len(pieces) == 3:
            claims = json.loads(base64.urlsafe_b64decode(pieces[1] + "=" * (-len(pieces[1]) % 4)))
            claims["exp"] = 0
            pieces[1] = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
            probe_access = ".".join(pieces)
        else:
            probe_access = "osscout-expired-access-probe"
        value["tokens"]["access_token"] = probe_access
        value["last_refresh"] = "2000-01-01T00:00:00Z"
        auth.write_text(json.dumps(value))
    try:
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=work, check=True)
            for read_only in (False, True):
                ok, output = CodexClient(timeout_seconds=180, workdir=work, read_only=read_only).run(
                    model + "#" + effort,
                    "This is an authentication smoke check. Do not edit files or run commands. Reply exactly OSS_SCOUT_OK.")
                if not ok or "OSS_SCOUT_OK" not in output:
                    # CodexClient already redacts the private login and rotated tokens.
                    raise RuntimeError("Codex session smoke failed: " + output)
                if not read_only and probe_access is not None:
                    fresh = json.loads(auth.read_text())
                    if (fresh["tokens"]["access_token"] == probe_access
                            or fresh.get("last_refresh") == "2000-01-01T00:00:00Z"):
                        raise RuntimeError("Native credential refresh was not observed")
                    print("Codex native credential refresh verified")
                print("Codex native session verified:", "read-only review" if read_only else "repair")
    finally:
        if probe_access is not None:
            try:
                fresh = json.loads(auth.read_text())
                valid = (fresh['tokens']['access_token'] != probe_access
                         and fresh.get('last_refresh') != '2000-01-01T00:00:00Z')
            except (OSError, ValueError, KeyError, TypeError):
                valid = False
            if not valid:
                # Never persist an artificial expired access token as ready.
                with open(auth.with_name('writeback-blocked'), 'w',
                          opener=lambda p, f: os.open(p, f, 0o600)) as target:
                    target.write('Native refresh verification requires recovery')


if __name__ == "__main__":
    main()
