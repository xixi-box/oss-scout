"""The account owner's identity for every automated upstream commit."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

AUTHOR_NAME = "wangshun"
AUTHOR_EMAIL = "96341168+xixi-box@users.noreply.github.com"
SIGN_OFF = f"Signed-off-by: {AUTHOR_NAME} <{AUTHOR_EMAIL}>"


def commit_as_owner(repo: Path, message: str) -> subprocess.CompletedProcess:
    # GIT_AUTHOR_*/GIT_COMMITTER_* override user.name/email, including when a
    # coding CLI left its own identity in the inherited environment.
    env = dict(os.environ, GIT_AUTHOR_NAME=AUTHOR_NAME, GIT_AUTHOR_EMAIL=AUTHOR_EMAIL,
               GIT_COMMITTER_NAME=AUTHOR_NAME, GIT_COMMITTER_EMAIL=AUTHOR_EMAIL)
    return subprocess.run([
        "git", "-c", "core.hooksPath=/dev/null", "-c", f"user.name={AUTHOR_NAME}",
        "-c", f"user.email={AUTHOR_EMAIL}", "commit", "-qm",
        message.rstrip() + "\n\n" + SIGN_OFF,
    ], cwd=repo, env=env, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=300)
