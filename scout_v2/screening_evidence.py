"""Read complete repository policies and current prerequisite facts before triage.

Extraction only selects references to look up. It never declares a security bug
or a mandatory dependency; the semantic scorer receives the original discussion.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import re
import subprocess
import threading
import time
from urllib.parse import quote
from pathlib import Path

from .contribution_intent import prose_clauses


CONTEXT_VERSION = 2
_CACHE = {}
_LOCK = threading.Lock()
_POLICY_TTL = 600
_PR_TTL = 30
_DEPENDENCY_HINT = re.compile(
    r"\b(?:depend\w*|prerequisites?|blocked\s+by|wait\w*|after|before|until|sequence|settle\w*|land\w*|needs?|requires?)\b"
    r"|(?:依赖|前置|等待|落定|先于|之后|再处理|合并后|落地后|阻塞|需先|需要先|先合并|后再|基于)", re.I)
_PR_REFERENCE = re.compile(
    r"https://github\.com/(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(?P<url_number>\d+)"
    r"|(?<![\w/])(?:(?P<short_repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+))?#(?P<number>\d+)")


class ScreeningEvidenceUnavailable(RuntimeError):
    """A temporary read failure must not become an affirmative or negative verdict."""


def _get(path: str, *, optional=False):
    try:
        response = subprocess.run(["gh", "api", path], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=20)
        value = json.loads(response.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise ScreeningEvidenceUnavailable("仓库规则/依赖证据读取失败，待重试") from None
    if response.returncode:
        if optional and isinstance(value, dict) and str(value.get("status")) == "404":
            return None  # Confirmed absence, unlike timeout/auth/rate-limit failures.
        status = str(value.get("status") or "unknown") if isinstance(value, dict) else "unknown"
        raise ScreeningEvidenceUnavailable(f"仓库规则/依赖证据读取失败（HTTP {status}），待重试")
    return value


def _cached(key, ttl, loader):
    now = time.monotonic()
    with _LOCK:
        record = _CACHE.get(key)
        if record and record[0] > now:
            if isinstance(record[1], ScreeningEvidenceUnavailable):
                raise ScreeningEvidenceUnavailable(str(record[1]))
            return copy.deepcopy(record[1])
    try:
        value = loader()
    except ScreeningEvidenceUnavailable as error:
        with _LOCK:
            _CACHE[key] = (time.monotonic() + 15, error)
        raise
    with _LOCK:
        if len(_CACHE) >= 512:
            for old in sorted(_CACHE, key=lambda entry: _CACHE[entry][0])[:128]:
                _CACHE.pop(old, None)
        _CACHE[key] = (time.monotonic() + ttl, value)
    return copy.deepcopy(value)


def _document(repo, path, ref=None):
    suffix = "?ref=" + quote(ref, safe="") if ref else ""
    document = _get(f"repos/{repo}/contents/{path}{suffix}", optional=True)
    if document is None:
        return None
    if (not isinstance(document, dict) or document.get("type") != "file"
            or document.get("encoding") != "base64" or not document.get("sha")
            or type(document.get("size")) is not int or document["size"] > 1_000_000):
        raise ScreeningEvidenceUnavailable("仓库规则无法完整读取，待核查/重试")
    try:
        raw = base64.b64decode(re.sub(r"\s+", "", document["content"]), validate=True)
        text = raw.decode("utf-8-sig")
    except (KeyError, ValueError, UnicodeError, TypeError):
        raise ScreeningEvidenceUnavailable("仓库规则内容不完整，待重试") from None
    if len(raw) != document["size"]:
        raise ScreeningEvidenceUnavailable("仓库规则内容长度不符，待重试")
    return {"repository": repo, "path": path, "sha": document["sha"], "content": text}


def _edge_evidence(route, payload):
    # Production controller opts in via its Cloudflare configuration; repair
    # runners keep their independent direct GitHub evidence path.
    root = os.environ.get("OSSCOUT_DASHBOARD_ROOT")
    if not root:
        return None
    from .edge_reads import EdgeRateLimited, edge_read
    try:
        return edge_read(Path(root), route, payload)
    except EdgeRateLimited as error:
        raise ScreeningEvidenceUnavailable("Cloudflare GitHub 限流，待重试") from error


def repository_policy_documents(repo: str) -> dict:
    def load():
        edge = _edge_evidence("repository-policy", {"repo": repo})
        if edge is not None:
            docs, absent = edge.get("documents"), edge.get("confirmed_absent")
            if (edge.get("complete") is not True or not isinstance(docs, list)
                    or not isinstance(absent, list) or any(not isinstance(row, dict) for row in docs)
                    or sorted([row.get("kind", "") for row in docs] + absent) != ["contributing", "security"]
                    or any(not all(isinstance(row.get(key), str) for key in
                                   ("repository", "path", "sha", "content")) for row in docs)):
                raise ScreeningEvidenceUnavailable("Cloudflare 仓库规则证据不完整，待重试")
            return edge
        metadata = _get(f"repos/{repo}")
        if not isinstance(metadata, dict) or not isinstance(metadata.get("default_branch"), str):
            raise ScreeningEvidenceUnavailable("仓库默认分支信息不可用，待重试")
        branch = metadata["default_branch"]
        head = _get(f"repos/{repo}/commits/{quote(branch, safe='')}")
        if not isinstance(head, dict) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", str(head.get("sha") or "")):
            raise ScreeningEvidenceUnavailable("仓库规则版本信息不可用，待重试")
        documents, absent = [], []
        for kind, candidates in (
            ("security", ("SECURITY.md", ".github/SECURITY.md", "docs/SECURITY.md")),
            ("contributing", ("CONTRIBUTING.md", ".github/CONTRIBUTING.md", "docs/CONTRIBUTING.md",
                              "CONTRIBUTING.rst", "CONTRIBUTING")),
        ):
            found = None
            for path in candidates:
                found = _document(repo, path, head["sha"])
                if found:
                    break
            if found is None:
                # GitHub can inherit an organization's default community files.
                default_repo = repo.split("/", 1)[0] + "/.github"
                found = _document(default_repo, "SECURITY.md" if kind == "security" else "CONTRIBUTING.md")
            if found:
                documents.append({"kind": kind, **found})
            else:
                absent.append(kind)
        return {"complete": True, "documents": documents, "confirmed_absent": absent}
    return _cached(("policy", repo.lower()), _POLICY_TTL, load)


def render_repository_policy(repo: str) -> str:
    policy = repository_policy_documents(repo)
    parts = []
    for document in policy["documents"]:
        parts.append(f"Repository {document['kind']} policy: {document['repository']}/{document['path']} "
                     f"(Git blob {document['sha']})\n{document['content']}")
    if policy["confirmed_absent"]:
        parts.append("Confirmed absent after checking repository and default community locations: "
                     + ", ".join(policy["confirmed_absent"]))
    return "\n\n".join(parts)


def _reference_state(repo, number):
    def load():
        # References may point to repositories outside the monitored roster.
        # Keep those on the direct public GitHub read path.
        from .roster import monitored_repo
        edge = _edge_evidence("reference-fact", {"repo": repo, "number": number}) if monitored_repo(repo) else None
        if edge is not None:
            fact = edge.get("fact")
            if (edge.get("complete") is not True or not isinstance(fact, dict)
                    or str(fact.get("repo", "")).lower() != repo.lower() or fact.get("number") != number
                    or fact.get("state") not in ("open", "closed")
                    or fact.get("kind") not in ("issue", "pull_request")
                    or (fact["kind"] == "pull_request" and type(fact.get("merged")) is not bool)):
                raise ScreeningEvidenceUnavailable("Cloudflare 前置依赖证据不完整，待重试")
            return fact
        pull = _get(f"repos/{repo}/pulls/{number}", optional=True)
        if pull is None:
            issue = _get(f"repos/{repo}/issues/{number}")
            if not isinstance(issue, dict) or issue.get("pull_request") or issue.get("state") not in ("open", "closed"):
                raise ScreeningEvidenceUnavailable("前置依赖状态不可用，待重试")
            return {"repo": repo, "number": number, "kind": "issue", "state": issue["state"],
                    "state_reason": issue.get("state_reason"), "url": issue.get("html_url")}
        if (not isinstance(pull, dict) or pull.get("state") not in ("open", "closed")
                or "merged_at" not in pull):
            raise ScreeningEvidenceUnavailable("前置 PR 合并状态不完整，待重试")
        return {"repo": repo, "number": number, "kind": "pull_request", "state": pull["state"],
                "merged": bool(pull.get("merged_at")), "url": pull.get("html_url"),
                "base_ref": (pull.get("base") or {}).get("ref"),
                "head_sha": (pull.get("head") or {}).get("sha")}
    return _cached(("reference", repo.lower(), number), _PR_TTL, load)


def prerequisite_facts(item: dict) -> list[dict]:
    repo, number = str(item["repo"]), int(item["number"])
    sources = [{"author": item.get("reporter"), "association": item.get("reporter_association"),
                "author_type": item.get("reporter_type"), "body": item.get("body_excerpt") or ""},
               *(item.get("issue_comments") or [])]
    references = {}
    for source in sources:
        text = str(source.get("body") or "")
        text = re.sub(r"(?ms)^\s*(`{3,}|~{3,})[^\n]*\n.*?^\s*\1[^\n]*$", "", text)
        text = re.sub(r"`([^`\n]+)`", r"\1", text)
        for clause in prose_clauses(text):
            if not _DEPENDENCY_HINT.search(clause):
                continue
            for match in _PR_REFERENCE.finditer(clause):
                target_repo = match["repo"] or match["short_repo"] or repo
                target_number = int(match["url_number"] or match["number"])
                if target_repo.lower() == repo.lower() and target_number == number:
                    continue
                key = (target_repo.lower(), target_number)
                references.setdefault(key, []).append({"author": source.get("author"),
                    "association": source.get("association"), "author_type": source.get("author_type"),
                    "url": source.get("url"), "text": clause})
    if len(references) > 8:
        raise ScreeningEvidenceUnavailable("前置依赖过多，无法完整核实，待核查/重试")
    return [{**_reference_state(target_repo, target_number), "mentions": mentions}
            for (target_repo, target_number), mentions in sorted(references.items())]


def build_screening_context(item: dict, rules: str) -> dict:
    references = prerequisite_facts(item)
    original = {key: item.get(key) for key in (
        "title", "body_excerpt", "reporter", "reporter_association", "reporter_type", "issue_comments")}
    digest = hashlib.sha256(json.dumps({"original": original, "rules": rules, "prerequisites": references},
                                       sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {"screening_context_version": CONTEXT_VERSION, "screening_evidence_sha256": digest,
            "repo_rules": rules, "prerequisite_pull_requests": references}
