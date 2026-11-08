"""Durable, per-job approval for changes to an existing PR."""
from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

from .git_host import gh_pages, gh_read
from .roster import monitored_repo
from .state_store import queue_transaction

JOB_ID = re.compile(r"prfix-[0-9a-f]{32}")
SHA = re.compile(r"[0-9a-f]{40}")
RETENTION_SECONDS = 7 * 24 * 60 * 60


def _paths(root: Path, job_id: str) -> tuple[Path, Path]:
    if not JOB_ID.fullmatch(job_id):
        raise ValueError("invalid approval ID")
    directory = root / "artifacts" / "pr-approvals"
    return directory / f"{job_id}.json", directory / f"{job_id}.bundle"


def cleanup(root: Path) -> None:
    """Expire packages and records, and scrub drafts left by older releases."""
    directory = root / "artifacts" / "pr-approvals"
    if not directory.is_dir():
        return
    now = time.time()
    with sqlite3.connect(root / "state" / "pr_approval_publish.sqlite3", timeout=0) as lock:
        lock.execute("BEGIN IMMEDIATE")
        for path in directory.glob("prfix-*"):
            match = re.match(r"(prfix-[0-9a-f]{32})(?:\.(?:json|bundle|tmp).*)$", path.name)
            if not match:
                continue
            metadata, bundle = _paths(root, match[1])
            with queue_transaction(root, path=metadata, backup=False) as record:
                if path == metadata and record:
                    expires = record.get("expires_at") or path.stat().st_mtime + RETENTION_SECONDS
                    if expires <= now:
                        bundle.unlink(missing_ok=True)
                        path.unlink(missing_ok=True)
                        # Keep the in-memory record untouched so the transaction won't rewrite it.
                        continue
                    record["expires_at"] = expires
                    if "reply" in record:
                        record["has_reply"] = bool(record.pop("reply"))
                    if record.pop("approved_reply", None) and record.get("complete"):
                        record["reply_status"] = "sent"
                    if record.get("pushed") or record.get("complete"):
                        bundle.unlink(missing_ok=True)
                elif path.name.endswith(".bak") or path.name.endswith(".tmp"):
                    path.unlink(missing_ok=True)
                elif path == bundle and not record and path.exists():
                    if path.stat().st_mtime + RETENTION_SECONDS <= now:
                        path.unlink()


def review_targets(responses: list) -> list:
    targets = []
    seen = set()
    for response in responses:
        key = response["id"]
        if (not re.fullmatch(r"(inline|review|issue):[1-9][0-9]*", key)
                or key != f"{response['source']}:{response['thread_id']}" or key in seen):
            raise ValueError("invalid or duplicate reply target")
        seen.add(key)
        targets.append({**{field: response.get(field) for field in
                           ("id", "source", "thread_id", "url", "author", "body", "action",
                            "path", "line", "start_line", "original_line", "original_start_line")},
                        "has_draft": bool(response.get("draft"))})
    return targets


def stage(root: Path, result: dict) -> dict:
    job_id = str(result.get("job_id") or "")
    path, bundle_path = _paths(root, job_id)
    repo = str(result.get("repo") or "")
    fork = str(result.get("fork_repo") or "")
    branch = str(result.get("branch") or "")
    base = str(result.get("head_sha") or "")
    commit = str(result.get("commit_sha") or "")
    number = result.get("number")
    if (not monitored_repo(repo) or type(number) is not int or number <= 0
            or not re.fullmatch(r"xixi-box/[A-Za-z0-9_.-]+", fork)
            or not SHA.fullmatch(base) or (commit and not SHA.fullmatch(commit))
            or not branch or branch.startswith("-")):
        raise ValueError("invalid PR approval target")
    blob = base64.b64decode(result.get("bundle") or "", validate=True)
    if bool(commit) != bool(blob) or len(blob) > 6 * 1024 * 1024:
        raise ValueError("missing or oversized approval bundle")
    reply = str(result.get("reply") or "").strip()[:1000]
    targets = review_targets(result.get("responses") or [])
    verification_failed = result.get("verification_failed") is True
    if verification_failed and commit:
        raise ValueError("unverified patch cannot be staged for push")
    if not verification_failed and not commit and not reply and not any(target["has_draft"] for target in targets):
        return {}
    trigger = result.get("trigger") or {}
    path.parent.mkdir(parents=True, exist_ok=True)
    with queue_transaction(root, path=path, backup=False) as record:
        if not record:
            if blob:
                tmp = bundle_path.with_suffix(".tmp")
                tmp.write_bytes(blob)
                os.replace(tmp, bundle_path)
            record.update({k: result.get(k) for k in (
                "job_id", "run_id", "title", "summary", "test_result", "changed_files", "review_id")})
            record["verification_failed"] = verification_failed
            record.update(repo=repo, number=number, fork_repo=fork, branch=branch,
                          head_sha=base, commit_sha=commit, has_reply=bool(reply),
                          expires_at=time.time() + RETENTION_SECONDS,
                          source_kind=trigger.get("source"), comment_id=trigger.get("thread_id"),
                          source_author=trigger.get("author"), source_comment=trigger.get("body"),
                          source_url=trigger.get("url"))
            if targets:
                record["targets"] = targets
        return dict(record)


def _push(record: dict, bundle: Path) -> None:
    fork, branch = record["fork_repo"], record["branch"]
    base, commit = record["head_sha"], record["commit_sha"]
    with tempfile.TemporaryDirectory() as tmp:
        def git(*args):
            proc = subprocess.run(
                ["git", "-C", tmp, "-c", "credential.helper=",
                 "-c", "credential.helper=!gh auth git-credential", *args],
                capture_output=True, text=True, timeout=60,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
            if proc.returncode:
                raise RuntimeError(f"Git {args[0]} failed ({proc.returncode})")
            return proc.stdout.strip()

        git("init", "--bare", "--quiet")
        git("check-ref-format", f"refs/heads/{branch}")
        remote = f"https://github.com/{fork}.git"
        git("fetch", "--no-tags", "--depth=1", remote, f"refs/heads/{branch}")
        current = git("rev-parse", "FETCH_HEAD")
        if current == commit:
            return
        if current != base:
            raise RuntimeError("PR branch changed after testing")
        git("fetch", "--no-tags", str(bundle.resolve()), "refs/heads/oss-scout-approval")
        if git("rev-list", "--parents", "-n", "1", "FETCH_HEAD") != f"{commit} {base}":
            raise RuntimeError("approval bundle does not match the tested commit")
        git("push", remote, f"{commit}:refs/heads/{branch}")


def publish(root: Path, job_id: str, edited_reply: str | dict | None, post_comment) -> dict:
    path, bundle = _paths(root, job_id)
    # This lock covers network calls without blocking issue queue transactions.
    with sqlite3.connect(root / "state" / "pr_approval_publish.sqlite3", timeout=0) as lock:
        lock.execute("BEGIN IMMEDIATE")
        record = json.loads(path.read_text(encoding="utf-8"))
        if time.time() >= (record.get("expires_at") or path.stat().st_mtime + RETENTION_SECONDS):
            raise ValueError("审批已过期（保留 7 天）")
        if record.get("complete"):
            return {**record, "already_complete": True}
        if record.get("targets"):
            if not isinstance(edited_reply, dict):
                raise ValueError("飞书卡片没有提交评审回复")
            pending = [(t["id"], t["source"], t["thread_id"], edited_reply[t["id"]].strip()[:1000])
                       for t in record["targets"] if t["has_draft"] and t["id"] in edited_reply]
        else:
            has_reply = record.get("has_reply", bool(record.get("reply")))
            if has_reply and not isinstance(edited_reply, str):
                raise ValueError("飞书卡片没有提交回复字段，请重试")
            pending = [("", record["source_kind"], record["comment_id"], edited_reply.strip()[:1000])] if has_reply else []
        pending = [item for item in pending if item[3]]
        if not record["commit_sha"] and not pending:
            raise ValueError("请在飞书卡片中填写要发送的回复")
        repo, number = record["repo"], record["number"]
        if not monitored_repo(repo):
            raise ValueError("repository outside monitored roster")
        pr = gh_read([f"repos/{repo}/pulls/{number}"]) or {}
        head = pr.get("head") or {}
        if (pr.get("state") != "open" or (pr.get("user") or {}).get("login") != "xixi-box"
                or (head.get("repo") or {}).get("full_name", "").lower() != record["fork_repo"].lower()
                or head.get("ref") != record["branch"]
                or head.get("sha") not in {record["head_sha"], record["commit_sha"] or record["head_sha"]}):
            raise RuntimeError("PR closed or branch changed after testing")
        if record["commit_sha"] and not record.get("pushed"):
            _push(record, bundle)
            with queue_transaction(root, path=path, backup=False) as stored:
                stored["pushed"] = True
            bundle.unlink(missing_ok=True)
        statuses = dict(record.get("reply_statuses") or {})
        comments_by_kind = {}
        for target_id, source, thread_id, reply in pending:
            if target_id in statuses:
                continue
            inline = source == "inline"
            endpoint = f"repos/{repo}/{'pulls' if inline else 'issues'}/{number}/comments"
            marker = f"<!-- oss-scout-reply:{job_id}{':' + target_id if target_id else ''} -->"
            try:
                if inline not in comments_by_kind:
                    comments_by_kind[inline] = gh_pages(endpoint, 100)
                comments = comments_by_kind[inline]
                sent = comments is not None and any(
                    marker in (c.get("body") or "") and (c.get("user") or {}).get("login") == "xixi-box"
                    for c in comments)
                if comments is not None and not sent:
                    sent, _ = post_comment(repo, number, f"{reply}\n\n{marker}",
                                           kind=source, comment_id=thread_id)
                statuses[target_id] = "sent" if sent else "failed"
            except Exception:
                statuses[target_id] = "failed"
            with queue_transaction(root, path=path, backup=False) as stored:
                stored["reply_statuses"] = statuses
        with queue_transaction(root, path=path, backup=False) as stored:
            stored["complete"] = True
            stored["reply_status"] = ("failed" if "failed" in statuses.values() else
                                      "sent" if "sent" in statuses.values() else "none")
            stored.pop("reply", None)
            stored.pop("approved_reply", None)
            result = dict(stored)
        return result
