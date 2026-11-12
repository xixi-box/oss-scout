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


def test_followup_workflow_uses_same_owner_for_the_approval_bundle(
        repo_with_cli_identity, tmp_path, monkeypatch):
    repo = repo_with_cli_identity
    head = git(repo, "rev-parse", "HEAD")
    (repo / "source.txt").write_text("fixed\n", encoding="utf-8")
    git(repo, "add", "source.txt")
    tree = git(repo, "write-tree")
    (tmp_path / "prfix.json").write_text("##OSSCOUT_PRFIX##" + json.dumps(
        {"fixed": True, "tested_tree": tree, "test_result": "passed"}), encoding="utf-8")
    (tmp_path / "fix.json").write_text(json.dumps({"pr_number": 123}), encoding="utf-8")
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/oss-scout-pr-fix.yml").read_text(encoding="utf-8"))
    step = next(step for step in workflow["jobs"]["prfix"]["steps"]
                if step.get("name") == "Save tested commit without pushing")
    script = step["run"].split("python3 - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("HEAD_SHA", head)
    monkeypatch.setenv("GITHUB_WORKSPACE", str(root.parent))
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output"))
    monkeypatch.chdir(repo)
    exec(compile(script, "followup-stage", "exec"), {})
    assert_owner(repo)
    assert git(repo, "rev-parse", "HEAD^") == head
    assert git(repo, "rev-parse", "HEAD^{tree}") == tree
    assert (tmp_path / "pr-fix.bundle").is_file()
