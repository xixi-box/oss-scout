"""Real Git commits must keep the owner's author, committer, and DCO identity."""
import json
import subprocess
from pathlib import Path

import pytest
import yaml

from scout_v2.workers.commit_identity import AUTHOR_EMAIL, AUTHOR_NAME, SIGN_OFF, commit_as_owner


def git(repo, *args):
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


@pytest.fixture(params=["Codex", "OpenCode"])
def repo_with_cli_identity(request, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    tool = request.param
    git(repo, "config", "user.name", tool)
    git(repo, "config", "user.email", "tool@example.invalid")
    for kind in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{kind}_NAME", tool)
        monkeypatch.setenv(f"GIT_{kind}_EMAIL", "tool@example.invalid")
    (repo / "source.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", "source.txt")
    git(repo, "commit", "-qm", "base")
    return repo


def assert_owner(repo):
    assert git(repo, "show", "-s", "--format=%an|%ae|%cn|%ce") == (
        f"{AUTHOR_NAME}|{AUTHOR_EMAIL}|{AUTHOR_NAME}|{AUTHOR_EMAIL}")
    message = git(repo, "show", "-s", "--format=%B")
    assert message.count("Signed-off-by:") == 1
    assert message.endswith(SIGN_OFF)
    assert "tool@example.invalid" not in message


def test_commit_overrides_cli_environment_and_repository_identity(repo_with_cli_identity):
    repo = repo_with_cli_identity
    (repo / "source.txt").write_text("fixed\n", encoding="utf-8")
    git(repo, "add", "source.txt")
    commit_as_owner(repo, "fix: regression").check_returncode()
    assert_owner(repo)
