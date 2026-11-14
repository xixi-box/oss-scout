"""Read-only publication checks and a credential-free verified delivery bundle."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

from ..git_host import assignment_facts, gh_read, recheck_candidate
from .quality_gate import check_delivery_scope, check_patch_path, patch_files


_OPEN_PR_CAPS = {"nousresearch/hermes-agent": 5}


def _valid_repo(value) -> bool:
    return bool(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", value)
                and value.rsplit("/", 1)[1] not in (".", ".."))


def check_open_pr_capacity(repo: str, actor: str, cap: int | None = None, *, has_write: bool = False) -> dict:
    """Count open PRs including drafts only where a repository cap is known.

    Hermes' five-open-PR cap is documented by contributors in #131859 and
    github.com/cli/cli/issues/14874. Unknown repositories have no guessed cap.
    This reads state only; it never creates a probe PR or closes existing PRs.
    """
    limit = (_OPEN_PR_CAPS.get(repo.lower()) if not has_write else None) if cap is None else cap
    result = {"ok": True, "count": None, "limit": limit}
    if limit is None:
        return result
    if type(limit) is not int or limit < 1:
        return {**result, "ok": False, "error": "repository open PR cap is invalid",
                "failure_kind": "publication_evidence_unavailable"}
    try:
        response = subprocess.run([
            "gh", "pr", "list", "--repo", repo, "--author", actor,
            "--state", "open", "--limit", str(limit + 1), "--json", "number,isDraft",
        ], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45)
        if response.returncode:
            raise ValueError("open PR count request failed")
        rows = json.loads(response.stdout)
        if (not isinstance(rows, list) or any(not isinstance(row, dict)
                                            or type(row.get("number")) is not int
                                            or row["number"] <= 0 for row in rows)
                or len({row["number"] for row in rows}) != len(rows)):
            raise ValueError("open PR count response is incomplete")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {**result, "ok": False, "error": "repository open PR count unavailable",
                "failure_kind": "publication_evidence_unavailable"}
    result["count"] = len(rows)
    if len(rows) >= limit:
        reason = f"仓库同时开放PR已达{limit}个"
        result.update(ok=False, error=reason, coarse_reason=reason,
                      failure_kind="publication_open_pr_cap")
    return result


def refresh_publication_evidence(job_dict: dict, cfg: dict) -> dict:
    """Refresh full issue evidence without attempting any GitHub write.

    ``ok`` means necessary read-only checks passed, not that CreatePullRequest
    permission is guaranteed. The complete snapshot is intended for code review.
    """
    repo = str(job_dict.get("repo") or "")
    upstream = str(cfg.get("upstream") or "")
    push_repo = str(cfg.get("push_repo") or "")
    owner = push_repo.split("/", 1)[0]
    number = cfg.get("issue_number")
    result = {"schema_version": 1, "ok": False, "job_id": job_dict.get("job_id"),
              "base_sha": job_dict.get("base_sha"), "repo": repo, "issue_number": number,
              "checked_at": dt.datetime.now(dt.timezone.utc).isoformat()}

    def blocked(reason, *, incomplete=False):
        result.update(error=reason, failure_kind=("publication_evidence_unavailable" if incomplete
                                                 else "publication_preflight_blocked"))
        if not incomplete:
            result["coarse_reason"] = reason
        return result

    if (not _valid_repo(repo) or upstream.lower() != repo.lower()
            or not _valid_repo(push_repo)
            or type(number) is not int or number <= 0):
        return blocked("publication target or issue number is invalid")
    try:
        actor = gh_read(["user"])
        repository = gh_read([f"repos/{repo}"])
        if (not isinstance(actor, dict) or not isinstance(actor.get("login"), str)
                or not actor["login"] or not isinstance(repository, dict)
                or str(repository.get("full_name") or "").lower() != repo.lower()
                or any(type(repository.get(key)) is not bool for key in ("archived", "disabled"))):
            return blocked("publication account or repository evidence unavailable", incomplete=True)
        result["diagnostics"] = {"actor": actor["login"],
                                 "upstream_archived": repository.get("archived"),
                                 "upstream_disabled": repository.get("disabled"),
                                 "read_permissions": repository.get("permissions") or {},
                                 "pull_request_creation": "not_probed"}
        if actor["login"].lower() != owner.lower():
            return blocked("publication account does not match fork owner")
        if repository.get("archived") or repository.get("disabled"):
            return blocked("publication repository is archived or disabled")
        permissions = repository.get("permissions") or {}
        capacity = check_open_pr_capacity(repo, actor["login"], cfg.get("max_open_prs"),
                                          has_write=any(permissions.get(role) is True for role in ("push", "maintain", "admin")))
        result["diagnostics"]["open_pr_capacity"] = capacity
        if not capacity["ok"]:
            return {**result, **{key: capacity[key] for key in (
                "ok", "error", "failure_kind", "coarse_reason") if key in capacity}}
        latest = recheck_candidate(repo, number, my_login=owner, use_title_keywords=True)
    except (OSError, ValueError, RuntimeError, TypeError, KeyError):
        return blocked("publication evidence refresh failed", incomplete=True)
    if latest is None or not latest.complete or latest.errors or latest.screening_source is None:
        return blocked("publication issue, comments, timeline or PR search is incomplete", incomplete=True)
    source = latest.screening_source
    if (not isinstance(source, (tuple, list)) or len(source) != 3
            or not isinstance(source[0], dict) or not isinstance(source[1], list)
            or not isinstance(source[2], list)):
        return blocked("publication discussion snapshot is incomplete", incomplete=True)
    issue, comments, timeline = source
    if (issue.get("number") != number or issue.get("state") not in ("open", "closed")
            or not isinstance(issue.get("assignees"), list)
            or not isinstance(issue.get("labels"), list)
            or not all(isinstance(item, dict) for item in comments + timeline)):
        return blocked("publication issue metadata is incomplete", incomplete=True)
    result["snapshot"] = {"issue": issue, "comments": comments, "timeline": timeline,
                          "confirmed_prs": list(latest.linked_pr_urls) + list(latest.open_pr_hits),
                          "similar_prs": list(latest.similar_prs)}
    rule = cfg.get("assignment_rule") or {}
    if not isinstance(rule, dict):
        return blocked("publication assignment rule is unavailable", incomplete=True)
    facts = assignment_facts(issue, rule, owner)
    labels = {str(item.get("name") if isinstance(item, dict) else item).lower()
              for item in issue["labels"]}
    if not latest.issue_open or issue["state"] != "open":
        return blocked("Issue 已关闭")
    if latest.is_duplicate or labels & {"duplicate", "重复", "kind/duplicate", "type: duplicate"}:
        return blocked("Issue 已标记重复")
    if facts["assigned_to_others"]:
        return blocked("已有指派给其他人")
    if rule.get("requires_assignment") and not (facts["assigned_to_me"] or facts["assignment_exempt"]):
        return blocked(f"仓库要求指派给 {owner}")
    if latest.has_claim_signal:
        return blocked("claim_signal")
    if latest.has_cross_ref_pr or latest.open_pr_hits:
        return blocked("已有修复 PR · " + ", ".join(result["snapshot"]["confirmed_prs"]))
    result["ok"] = True
    return result


_BUNDLE_FILES = ("change.patch", "result.json", "test-report.json", "code-review.json",
                 "publication-evidence.json", "publish.json",
                 "base-regression.junit.xml", "patched-regression.junit.xml")
_RAW_BUNDLE_FILES = {"change.patch", "base-regression.junit.xml", "patched-regression.junit.xml"}
_JOB_FIELDS = ("job_id", "repo", "base_sha", "mode", "repair_agent", "model",
               "candidate_id", "idempotency_key")
_PUBLICATION_FIELDS = ("upstream", "push_repo", "branch", "issue_number", "base_branch")


def _redact(value, secrets):
    if isinstance(value, dict):
        return {key: ("[REDACTED]" if re.search(r"(?i)token|secret|password|credential|authorization|api.?key", key)
                      else _redact(item, secrets)) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[REDACTED]")
    return value


def _regular_file(path: Path, root: Path) -> bool:
    return (not path.is_symlink() and path.resolve().parent == root
            and stat.S_ISREG(path.stat().st_mode) and path.stat().st_nlink == 1)


def _verified_patch_digest(job: dict, files: dict) -> str:
    patch = files["change.patch"]
    result = json.loads(files["result.json"])
    report = json.loads(files["test-report.json"])
    if not isinstance(result, dict) or not isinstance(report, dict):
        raise ValueError("delivery metadata is not an object")
    digest = hashlib.sha256(patch).hexdigest()
    paths = patch_files(patch.decode("utf-8"))
    if (not re.fullmatch(r"job-[\w-]+", str(job.get("job_id") or ""))
            or not _valid_repo(job.get("repo"))
            or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", str(job.get("base_sha") or ""))
            or any(data.get("job_id") != job["job_id"] or data.get("base_sha") != job["base_sha"]
                   for data in (result, report))
            or result.get("outcome") != "succeeded"
            or not all(result.get(key) is True for key in (
                "reproduced_on_base", "fixed_on_patch", "regression_tests_passed"))
            or type(report.get("base_returncode")) is not int or report["base_returncode"] in (0, 124)
            or type(report.get("patched_returncode")) is not int or report["patched_returncode"] != 0
            or report.get("failure_kind") or report.get("delivery_patch_sha256") != digest
            or not paths or set(report.get("delivery_files") or []) != set(paths)
            or not report.get("regression_test_files")
            or not set(report["regression_test_files"]) <= set(paths)
            or any(check_patch_path(path) for path in paths)
            or check_delivery_scope(patch.decode("utf-8"))
            or not report.get("test_cmd") or not str(report.get("root_cause") or "").strip()):
        raise ValueError("delivery is not verified or lineage/hash/manifest differs")
    return digest


def _source_metadata(job: dict) -> dict:
    source = {"source_repository": job.get("runner_repo") or "xixi-box/oss-scout"}
    if not _valid_repo(source["source_repository"]):
        raise ValueError("invalid Actions source repository")
    if job.get("source_run") is not None:
        if not re.fullmatch(r"[1-9]\d*", str(job["source_run"])):
            raise ValueError("invalid Actions source run")
        source["source_run"] = str(job["source_run"])
    return source


def preserve_verified_bundle(job_dict: dict, out_dir: Path, publish_result: dict) -> dict:
    """Save a verified, allowlisted bundle; never copy credentials or a workspace.

    Return ``{ok, path, patch_sha256, manifest}``, or ``{ok: False, error}``.
    Each saved file is hashed in the manifest. An existing different/tampered
    bundle is rejected rather than overwritten. No automatic publication occurs.
    """
    try:
        out_dir = Path(out_dir)
        if out_dir.is_symlink() or not out_dir.is_dir():
            raise ValueError("unsafe artifact directory")
        root = out_dir.resolve()
        files = {}
        for name in _BUNDLE_FILES:
            path = root / name
            if path.is_symlink() or (path.exists() and not _regular_file(path, root)):
                raise ValueError(f"unsafe delivery artifact: {name}")
            if path.exists():
                if name.endswith(".junit.xml") and path.stat().st_size > 5_000_000:
                    raise ValueError(f"structured test report is too large: {name}")
                files[name] = path.read_bytes()
        digest = _verified_patch_digest(job_dict, files)
        report = json.loads(files["test-report.json"])
        if not isinstance(publish_result, dict):
            raise ValueError("delivery metadata is not an object")
        for stage in ("base", "patched"):
            name = f"{stage}-regression.junit.xml"
            evidence = report.get(f"{stage}_evidence") or {}
            if not isinstance(evidence, dict):
                raise ValueError("structured test report metadata is not an object")
            if evidence.get("format") == "junit":
                if (name not in files or evidence.get("artifact") != name
                        or hashlib.sha256(files[name]).hexdigest() != evidence.get("sha256")):
                    raise ValueError("structured test report does not match verification hash")
            elif name in files:
                raise ValueError("structured test report has no matching verification metadata")
        secrets = [value for key, value in os.environ.items() if value
                   and re.search(r"(?i)(?:token|secret|password|api.?key)$", key)]
        if any(secret.encode() in raw for name, raw in files.items() if name in _RAW_BUNDLE_FILES for secret in secrets):
            raise ValueError("verified patch or structured test report contains a runtime secret")
        for name, raw in list(files.items()):
            if name not in _RAW_BUNDLE_FILES:
                files[name] = (json.dumps(_redact(json.loads(raw), secrets), ensure_ascii=False, indent=2) + "\n").encode()
        files["publish.json"] = (json.dumps(_redact(publish_result, secrets), ensure_ascii=False, indent=2) + "\n").encode()
        manifest = {"schema_version": 1, "job": _redact({key: job_dict.get(key) for key in _JOB_FIELDS}, secrets),
                    "publication": _redact({key: (job_dict.get("publish") or {}).get(key)
                                            for key in _PUBLICATION_FIELDS}, secrets),
                    **_source_metadata(job_dict), "patch_sha256": digest, "retention_days": 30,
                    "files": {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()}}
        target = root / "verified-delivery"
        if target.is_symlink():
            raise ValueError("unsafe verified delivery directory")
        if target.exists():
            if not target.is_dir() or target.resolve().parent != root:
                raise ValueError("unsafe verified delivery directory")
            if not _regular_file(target / "manifest.json", target):
                raise ValueError("unsafe verified delivery manifest")
            existing = json.loads((target / "manifest.json").read_bytes())
            if existing != manifest or {p.name for p in target.iterdir()} != set(files) | {"manifest.json"}:
                raise ValueError("existing verified delivery differs")
            for name, raw in files.items():
                if not _regular_file(target / name, target) or (target / name).read_bytes() != raw:
                    raise ValueError("existing verified delivery was modified")
        else:
            with tempfile.TemporaryDirectory(prefix=".verified-delivery-", dir=root) as staging:
                stage = Path(staging)
                for name, raw in files.items():
                    (stage / name).write_bytes(raw)
                (stage / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                os.replace(stage, target)
        return {"ok": True, "path": str(target), "patch_sha256": digest, "manifest": manifest}
    except (OSError, ValueError, TypeError, KeyError, UnicodeError) as exc:
        return {"ok": False, "error": f"verified delivery preservation refused: {type(exc).__name__}: {exc}"}


def retain_collected_delivery(root: Path, job_dict: dict, collected: dict) -> dict:
    """Retain a confirmed repair even when log fallback lacks the original XML.

    This is an archive, never an authorization to publish. Missing raw reports
    require independent re-verification. Optional raw files must be provided as
    bytes/text/JSON values under ``raw.artifact_files`` (or ``raw.files``), never
    as filesystem paths. Existing archives are verified and kept unchanged.
    """
    try:
        root = Path(root)
        if root.is_symlink() or not root.is_dir() or not isinstance(collected, dict):
            raise ValueError("unsafe archive root or collected result")
        raw = collected.get("raw") if isinstance(collected.get("raw"), dict) else {}
        result = collected.get("result") if isinstance(collected.get("result"), dict) else collected
        report = collected.get("test_report", raw.get("test_report"))
        if isinstance(report, (str, bytes)):
            report = json.loads(report)
        if not isinstance(report, dict):
            raise ValueError("collected verification report is missing")
        patch = collected.get("patch", raw.get("patch"))
        if isinstance(patch, str):
            patch = patch.encode("utf-8")
        if not isinstance(patch, bytes):
            raise ValueError("collected patch is missing")
        # A flattened Actions result also carries patch/report/raw. Do not persist
        # those transport envelopes or any arbitrary credential-bearing fields.
        result_fields = ("schema_version", "job_id", "backend", "outcome", "reproduced_on_base",
                         "fixed_on_patch", "regression_tests_passed", "patch_path", "test_report_path",
                         "logs_path", "base_sha", "model_used", "measured_cost_usd", "unverified_items")
        result = {key: result[key] for key in result_fields if key in result}
        encode = lambda value: (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
        files = {"change.patch": patch, "result.json": encode(result), "test-report.json": encode(report)}
        digest = _verified_patch_digest(job_dict, files)
        extra = raw.get("artifact_files", raw.get("files", collected.get("artifact_files", {})))
        if not isinstance(extra, dict):
            raise ValueError("collected artifact metadata is invalid")
        for name in _BUNDLE_FILES:
            if name in files or name not in extra:
                continue
            value = extra[name]
            if isinstance(value, bytes):
                files[name] = value
            elif isinstance(value, str):
                files[name] = value.encode("utf-8")
            elif isinstance(value, dict) and name.endswith(".json"):
                files[name] = encode(value)
            else:
                raise ValueError(f"invalid collected artifact content: {name}")
        publication = collected.get("publish", raw.get("publish"))
        if isinstance(publication, dict):
            files["publish.json"] = encode(publication)
        missing = False
        for stage in ("base", "patched"):
            name = f"{stage}-regression.junit.xml"
            evidence = report.get(f"{stage}_evidence") or {}
            if not isinstance(evidence, dict):
                raise ValueError("invalid collected structured report metadata")
            if name not in files:
                missing = True
            elif (len(files[name]) > 5_000_000 or evidence.get("format") != "junit"
                  or evidence.get("artifact") != name
                  or hashlib.sha256(files[name]).hexdigest() != evidence.get("sha256")):
                raise ValueError("collected structured report hash or size differs")
        secrets = [value for key, value in os.environ.items() if value
                   and re.search(r"(?i)(?:token|secret|password|api.?key)$", key)]
        if any(secret.encode() in value for name, value in files.items()
               if name in _RAW_BUNDLE_FILES for secret in secrets):
            raise ValueError("collected patch or structured report contains a runtime secret")
        for name in list(files):
            if name not in _RAW_BUNDLE_FILES:
                files[name] = encode(_redact(json.loads(files[name]), secrets))
        manifest = {"schema_version": 1, "job": _redact({key: job_dict.get(key) for key in _JOB_FIELDS}, secrets),
                    "publication": _redact({key: (job_dict.get("publish") or {}).get(key)
                                            for key in _PUBLICATION_FIELDS}, secrets),
                    **_source_metadata(job_dict), "patch_sha256": digest,
                    "raw_reports_missing": missing, "requires_reverification": missing,
                    "requires_publication_preflight": True,
                    "files": {name: hashlib.sha256(value).hexdigest() for name, value in files.items()}}
        parent = root.resolve()
        for directory in ("artifacts", "verified-deliveries"):
            path = parent / directory
            if path.is_symlink() or (path.exists() and (not path.is_dir() or path.resolve().parent != parent)):
                raise ValueError("unsafe archive directory")
            path.mkdir(exist_ok=True)
            parent = path
        target = parent / job_dict["job_id"]
        if target.is_symlink():
            raise ValueError("unsafe existing delivery archive")
        if target.exists():
            if not target.is_dir() or not _regular_file(target / "manifest.json", target):
                raise ValueError("unsafe existing delivery manifest")
            existing = json.loads((target / "manifest.json").read_bytes())
            identity = ("job_id", "repo", "base_sha")
            if (not isinstance(existing, dict) or not isinstance(existing.get("job"), dict)
                    or any(existing["job"].get(key) != manifest["job"].get(key) for key in identity)
                    or existing.get("patch_sha256") != digest
                    or existing.get("source_repository") != manifest["source_repository"]
                    or existing.get("source_run") != manifest.get("source_run")
                    or not isinstance(existing.get("files"), dict)
                    or not set(existing["files"]) <= set(_BUNDLE_FILES)
                    or set(existing["files"]) | {"manifest.json"} != {path.name for path in target.iterdir()}):
                raise ValueError("existing delivery archive has different lineage, source or files")
            existing_files = {}
            for name, expected in existing["files"].items():
                path = target / name
                if not _regular_file(path, target):
                    raise ValueError("unsafe existing delivery archive file")
                value = path.read_bytes()
                if hashlib.sha256(value).hexdigest() != expected:
                    raise ValueError("existing delivery archive was modified")
                existing_files[name] = value
            if any(existing_files.get(name) != files[name] for name in (
                    "change.patch", "result.json", "test-report.json")):
                raise ValueError("existing core delivery differs from the collected source")
            _verified_patch_digest(job_dict, existing_files)
            existing_report = json.loads(existing_files["test-report.json"])
            missing = False
            for stage in ("base", "patched"):
                name = f"{stage}-regression.junit.xml"
                evidence = existing_report.get(f"{stage}_evidence") or {}
                if name not in existing_files:
                    missing = True
                elif (not isinstance(evidence, dict) or evidence.get("format") != "junit"
                      or evidence.get("artifact") != name or len(existing_files[name]) > 5_000_000
                      or hashlib.sha256(existing_files[name]).hexdigest() != evidence.get("sha256")):
                    raise ValueError("existing structured report differs from verification evidence")
            if (existing.get("raw_reports_missing") is not missing
                    or existing.get("requires_reverification") is not missing
                    or existing.get("requires_publication_preflight") is not True):
                raise ValueError("existing archive verification flags were modified")
            manifest = existing
        else:
            with tempfile.TemporaryDirectory(prefix=".delivery-", dir=parent) as staging:
                stage = Path(staging)
                for name, value in files.items():
                    (stage / name).write_bytes(value)
                (stage / "manifest.json").write_bytes(encode(manifest))
                os.replace(stage, target)
        return {"ok": True, "path": str(target), "patch_sha256": digest, "manifest": manifest,
                "raw_reports_missing": manifest["raw_reports_missing"],
                "requires_reverification": manifest["requires_reverification"]}
    except (OSError, ValueError, TypeError, KeyError, UnicodeError) as exc:
        return {"ok": False, "error": f"collected delivery retention refused: {type(exc).__name__}: {exc}"}
