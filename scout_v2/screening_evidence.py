"""Read complete repository policies and current prerequisite facts before triage.

The coarse gate checks explicit opening conditions. The semantic scorer receives
the original discussion for requirements that need interpretation.
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
    r"https://github\.com/(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/(?:issues|pull)/(?P<url_number>\d+)"
    r"|(?<![\w/])(?:(?P<short_repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+))?#(?P<number>\d+)")


_DISCUSSION_REFERENCE = re.compile(
    r"(?:\bdiscussions?|讨论)\s*[:：]?\s*(?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#\d+",
    re.I,
)


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
            issue = _get(f"repos/{repo}/issues/{number}", optional=True)
            if issue is None:
                return {"repo": repo, "number": number, "kind": "unavailable", "state": "not_found"}
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


def coarse_prerequisite_blocker(item: dict) -> str | None:
    # Restrict the mechanical gate to an explicit, unconditional opening rule in
    # the issue body. A later authoritative comment may supersede it.
    if any((item.get("reporter") and comment.get("author") == item["reporter"])
           or comment.get("association") in {"OWNER", "MEMBER", "COLLABORATOR"}
           for comment in item.get("issue_comments") or []):
        return None
    body = re.sub(r"`(#\d+)`", r"\1", item.get("body_excerpt") or "")
    for clause in prose_clauses(body):
        prefix = re.match(r"^(?:[-*]\s+)?(?:\*\*)?(?:(?:硬依赖|Hard (?:dependency|prerequisite))"
                          r"(?:\*\*)?\s*[：:]\s*|(?P<bare>blocked by|depends on|依赖)(?:\s*[:：]\s*|\s+))(?:\*\*)?",
                          clause, re.I)
        if not prefix:
            continue
        reference = _PR_REFERENCE.match(clause, prefix.end())
        if not reference:
            continue
        condition = re.match(r"\s*(?:\*\*)?\s*(?:(关闭|合并)(?:并冻结[^。!?！？#或]*)?后(?:才(?:能|可)|方可)(?:开始|开工)"
                             r"|must (?:be )?(closed|merged|merge|close) before (?:starting|implementation))",
                             clause[reference.end():], re.I)
        bare = prefix["bare"] and clause[reference.end():].strip(" *.。") == ""
        if not condition and not bare:
            continue
        repo = reference["repo"] or reference["short_repo"] or item["repo"]
        number = int(reference["url_number"] or reference["number"])
        if repo.lower() == str(item["repo"]).lower() and number == int(item["number"]):
            continue
        try:
            fact = _reference_state(repo, number)
        except ScreeningEvidenceUnavailable as error:
            detail = str(error).split("，待", 1)[0]
            return f"明确前置依赖状态未确认 · {repo}#{number} · {detail}"
        if fact["state"] == "not_found":
            return f"明确前置依赖无法访问 · {repo}#{number} · GitHub HTTP 404"
        if fact["state"] == "open":
            return f"明确前置依赖尚未满足 · {repo}#{number} 仍开放"
        requires_merge = bool(bare) or (condition[1] or condition[2]).lower() in {"合并", "merge", "merged"}
        if requires_merge and fact["kind"] == "pull_request" and not fact.get("merged"):
            return f"明确前置依赖尚未满足 · {repo}#{number} 未合并"
    return None


def coarse_branch_scope_blocker(item: dict, scope: dict) -> str | None:
    branch, component, path = (scope[key] for key in ("branch", "component", "path"))
    title, body = item.get("title") or "", item.get("body_excerpt") or ""
    if not (re.search(rf"(?<![\w-]){re.escape(branch)}(?![\w/-])", body)
            or re.search(rf"(?<![A-Za-z0-9]){re.escape(component)}(?![A-Za-z0-9])", title, re.I)):
        return None
    repo = item["repo"]
    tree_entry = _cached(("default-scope", repo.lower(), path), _POLICY_TTL, lambda:
                         _get(f"repos/{repo}/contents/{quote(path, safe='/')}", optional=True))
    if tree_entry is None:
        return f"默认分支不含 {component} · 该问题涉及 {branch}"
    return None


def coarse_delivery_scope_blocker(item: dict) -> str | None:
    body = re.sub(r"(?ms)^\s*(`{3,}|~{3,})[^\n]*\n.*?^\s*\1[^\n]*$", "", item.get("body_excerpt") or "")
    requested = re.search(r"^#{2,}[ \t]+(?:requested fix|proposed changes|suggested fix|修复要求|建议修复)\s*$"
                          r"(.*?)(?=^#{2,}[ \t]|\Z)", body, re.I | re.M | re.S)
    if requested:
        action = requested[1].replace("`", "")
        if (re.search(r"\b(?:rulesets?|branch protection|required_status_checks)\b|分支保护|合并规则", action, re.I)
                and re.search(r"\b(?:needs? to be required|make .{1,70} required|(?:add|update|enable|configure) "
                              r"(?:the )?(?:repository )?(?:ruleset|branch protection|required status checks?))\b"
                              r"|(?:调整|启用|配置)(?:仓库)?(?:合并规则|分支保护)", action, re.I)):
            return "需要仓库管理员调整合并规则"
        if re.search(r"\b(?:update|edit|change|modify)\b[^\n]*\.github/(?:workflows|actions)/"
                     r"|(?:修改|调整)[^\n]*\.github/(?:workflows|actions)/", action, re.I):
            return "需要修改 CI 工作流"
        if (re.match(r"\s*ci(?:\([^)]*\))?:", item.get("title") or "", re.I)
                and re.search(r"\b(?:remove|change|update|add|move|replace|use)\b[^\n]*"
                              r"(?:\bneeds\b|job dependency|status-check condition)", action, re.I)):
            return "需要修改 CI 工作流"

    # A follow-up link alone is insufficient. Confirm the cited head and an
    # affected source file present on that head but absent on the default base.
    reported_head = re.search(r"\bhead\s+`?([0-9a-f]{7,40})\b", body, re.I)
    source = re.search(r"\bfound by [^\n.]{1,100}\breviewer on\s+`?", body, re.I)
    if not reported_head or not source or not re.search(r"\bfollow[- ]up\b", body, re.I):
        return None
    reference = _PR_REFERENCE.match(body, source.end())
    if not reference:
        return None
    repo, number = item["repo"], int(reference["url_number"] or reference["number"])
    target = reference["repo"] or reference["short_repo"] or repo
    if target.lower() != repo.lower() or number == int(item["number"]):
        return None
    fact = _reference_state(repo, number)
    head = str(fact.get("head_sha") or "").lower()
    if (fact.get("kind") != "pull_request" or fact.get("state") != "open" or fact.get("merged")
            or not re.fullmatch(r"[0-9a-f]{40,64}", head) or not head.startswith(reported_head[1].lower())):
        return None
    where = re.search(r"^#{2,}[ \t]+(?:where|affected code|受影响代码)\s*$"
                      r"(.*?)(?=^#{2,}[ \t]|\Z)", body, re.I | re.M | re.S)
    paths = re.findall(r"`((?:[\w-]+/)+[\w.-]+\.(?:py|go|rs|ts|tsx|js|mjs|java|cpp|c|cs))`", where[1] if where else "")
    for path in list(dict.fromkeys(paths))[:4]:
        endpoint = f"repos/{repo}/contents/{quote(path, safe='/')}"
        if _get(endpoint, optional=True) is None:
            source_file = _get(f"{endpoint}?ref={head}", optional=True)
            if isinstance(source_file, dict) and source_file.get("type") == "file":
                return f"默认分支不含受影响代码 · 前置 PR #{number} 尚未合并 · {path}"
    return None


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
            for match in _PR_REFERENCE.finditer(_DISCUSSION_REFERENCE.sub(" ", clause)):
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
    if item.get("issue_item_scope"):
        original["issue_item_scope"] = item["issue_item_scope"]
    digest = hashlib.sha256(json.dumps({"original": original, "rules": rules, "prerequisites": references},
                                       sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {"screening_context_version": CONTEXT_VERSION, "screening_evidence_sha256": digest,
            "repo_rules": rules, "prerequisite_pull_requests": references}
