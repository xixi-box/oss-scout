import base64
import subprocess

import pytest

from scout_v2 import pr_approval


def test_approval_pushes_exact_tested_commit_before_reply_and_recovers(tmp_path, monkeypatch):
    def git(where, *args):
        return subprocess.check_output(
            ["git", "-C", str(where), *args], text=True, stderr=subprocess.PIPE).strip()

    origin = tmp_path / "fork.git"
    origin.mkdir()
    git(origin, "init", "--bare")
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-b", "topic")
    git(work, "config", "user.name", "test")
    git(work, "config", "user.email", "test@example.com")
    (work / "code").write_text("base")
    git(work, "add", "code")
    git(work, "commit", "-m", "base")
    base = git(work, "rev-parse", "HEAD")
    git(work, "push", origin.as_uri(), "HEAD:refs/heads/topic")
    (work / "code").write_text("verified")
    git(work, "commit", "-am", "tested fix")
    commit = git(work, "rev-parse", "HEAD")
    git(work, "update-ref", "refs/heads/oss-scout-approval", commit)
    bundle = tmp_path / "test.bundle"
    git(work, "bundle", "create", str(bundle), "refs/heads/oss-scout-approval", f"^{base}")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{origin.as_uri()}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://github.com/xixi-box/compose.git")
    monkeypatch.setattr(pr_approval, "gh_read", lambda _: {
        "state": "open", "user": {"login": "xixi-box"},
        "head": {"repo": {"full_name": "xixi-box/compose"}, "ref": "topic",
                 "sha": git(origin, "rev-parse", "topic")},
    })
    monkeypatch.setattr(pr_approval, "gh_pages", lambda *_: [])
    job_id = "prfix-" + "1" * 32
    record = pr_approval.stage(tmp_path, {
        "job_id": job_id, "repo": "docker/compose", "number": 42,
        "fork_repo": "xixi-box/compose", "branch": "topic", "head_sha": base,
        "commit_sha": commit, "bundle": base64.b64encode(bundle.read_bytes()).decode(),
        "reply": "Fixed.", "trigger": {"source": "inline", "thread_id": 12},
    })
    assert not record.get("complete")
    assert git(origin, "rev-parse", "topic") == base
    posted = []

    def post(repo, number, body, **target):
        assert git(origin, "rev-parse", "topic") == commit
        posted.append((body, target))
        return len(posted) > 1, "temporary failure"

    with pytest.raises(RuntimeError, match="GitHub reply failed"):
        pr_approval.publish(tmp_path, job_id, "Edited reply", post)
    done = pr_approval.publish(tmp_path, job_id, "ignored on retry", post)
    assert done["pushed"] and done["complete"]
    assert posted[0] == posted[1]
    assert posted[0][0].startswith("Edited reply\n")
    assert posted[0][1] == {"kind": "inline", "comment_id": 12}
    assert pr_approval.publish(tmp_path, job_id, None, post)["complete"]
    assert len(posted) == 2
