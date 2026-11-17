"""Two serial CLI calls using a single local subscription session; no PR writes."""
import os
import json
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
    original_refresh = None
    if os.environ.get("CHECK_REFRESH") == "true":
        # Exercise the CLI's native rotation, never call OAuth ourselves.
        auth = Path(os.environ["OSSCOUT_CODEX_AUTH_FILE"])
        value = json.loads(auth.read_text())
        original_refresh = value["tokens"]["refresh_token"]
        value["last_refresh"] = "2000-01-01T00:00:00Z"
        auth.write_text(json.dumps(value))
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
            if not read_only and original_refresh is not None:
                if json.loads(auth.read_text())["tokens"]["refresh_token"] == original_refresh:
                    raise RuntimeError("Native refresh was requested but rotation was not observed")
                print("Codex native credential rotation verified")
            print("Codex native session verified:", "read-only review" if read_only else "repair")


if __name__ == "__main__":
    main()
