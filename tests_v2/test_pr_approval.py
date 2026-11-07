import base64
import subprocess

from scout_v2 import pr_approval


def test_approval_deletes_package_before_reply_and_never_saves_draft(tmp_path, monkeypatch):
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
    metadata, package = pr_approval._paths(tmp_path, job_id)
    assert "Fixed." not in metadata.read_text(encoding="utf-8")
    assert git(origin, "rev-parse", "topic") == base
    posted = []

    def post(repo, number, body, **target):
        assert git(origin, "rev-parse", "topic") == commit
        assert not package.exists()
        posted.append((body, target))
        return False, "temporary failure"

    done = pr_approval.publish(tmp_path, job_id, "Edited reply", post)
    assert done["pushed"] and done["complete"]
    assert done["reply_status"] == "failed"
    assert "Edited reply" not in metadata.read_text(encoding="utf-8")
    assert not metadata.with_suffix(".json.bak").exists()
    assert posted[0][0].startswith("Edited reply\n")
    assert posted[0][1] == {"kind": "inline", "comment_id": 12}
    assert pr_approval.publish(tmp_path, job_id, None, post)["complete"]
    assert len(posted) == 1


def test_cleanup_scrubs_legacy_and_expires_record_and_package(tmp_path, monkeypatch):
    import json
    import os

    job_id = "prfix-" + "2" * 32
    path, bundle = pr_approval._paths(tmp_path, job_id)
    (tmp_path / "state").mkdir()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"reply": "legacy draft", "approved_reply": "legacy edit"}))
    os.utime(path, (100, 100))
    backup = path.with_suffix(".json.bak")
    backup.write_text("legacy draft")
    bundle.write_bytes(b"package")
    expires = 100 + pr_approval.RETENTION_SECONDS
    monkeypatch.setattr(pr_approval.time, "time", lambda: expires - 1)
    pr_approval.cleanup(tmp_path)
    assert json.loads(path.read_text()) == {"has_reply": True, "expires_at": expires}
    assert bundle.exists() and not backup.exists()
    monkeypatch.setattr(pr_approval.time, "time", lambda: expires)
    pr_approval.cleanup(tmp_path)
    assert not path.exists() and not bundle.exists()


def test_one_review_push_then_only_selected_threads(tmp_path, monkeypatch):
    from scout_v2.feishu_app import build_pr_approval_card
    job_id = "prfix-" + "3" * 32
    head = "a" * 40
    responses = [
        {"id": f"inline:{i}", "source": "inline", "thread_id": i, "author": "maintainer",
         "body": "```suggestion\n    new_code()\n```", "action": "change", "draft": f"Draft {i}"}
        for i in range(1, 6)
    ]
    record = pr_approval.stage(tmp_path, {
        "job_id": job_id, "repo": "python/cpython", "number": 161543,
        "fork_repo": "xixi-box/cpython", "branch": "topic", "head_sha": head,
        "commit_sha": "b" * 40, "bundle": "cGFja2FnZQ==", "responses": responses,
    })
    metadata, bundle = pr_approval._paths(tmp_path, job_id)
    assert "Draft 1" not in metadata.read_text(encoding="utf-8")
    card = build_pr_approval_card(record, drafts={r["id"]: r["draft"] for r in responses}, preview=True)
    form = card["elements"][-1]["elements"]
    assert len(form[0]["options"]) == 5
    assert [part["name"] for part in form if part["tag"] == "input"] == [f"reply_{i}" for i in range(5)]
    assert "```suggestion" not in str(card)
    assert form[-1]["value"]["do"] == "preview_pr_followup"
    monkeypatch.setattr(pr_approval, "gh_read", lambda _: {
        "state": "open", "user": {"login": "xixi-box"},
        "head": {"repo": {"full_name": "xixi-box/cpython"}, "ref": "topic", "sha": head},
    })
    monkeypatch.setattr(pr_approval, "gh_pages", lambda *_: [])
    events = []
    monkeypatch.setattr(pr_approval, "_push", lambda *_: events.append("push"))

    def post(repo, number, text, *, kind, comment_id):
        assert not bundle.exists()
        assert kind == "inline"
        events.append((comment_id, text.splitlines()[0]))
        return comment_id == 2, ""

    result = pr_approval.publish(tmp_path, job_id, {"inline:2": "Edited two", "inline:5": "Edited five"}, post)
    assert events == ["push", (2, "Edited two"), (5, "Edited five")]
    assert result["reply_statuses"] == {"inline:2": "sent", "inline:5": "failed"}
    assert "Edited" not in metadata.read_text(encoding="utf-8")
