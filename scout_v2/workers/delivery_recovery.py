"""Recover a retained agent repair through a new reservation and mechanical preflight.

This module cannot publish. The ordinary worker and publication gates
remain responsible for every recovered delivery.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from .publication_guard import _BUNDLE_FILES, _regular_file, _verified_patch_digest, _valid_repo


def validate_source(source: dict) -> None:
    if (not isinstance(source, dict) or not _valid_repo(source.get("repository"))
            or not re.fullmatch(r"job-[\w-]+", str(source.get("job_id") or ""))
            or not re.fullmatch(r"[1-9]\d*", str(source.get("run_id") or ""))
            or not re.fullmatch(r"[a-f0-9]{64}", str(source.get("patch_sha256") or ""))):
        raise ValueError("invalid retained delivery source")


def load_bundle(directory: Path, source: dict, *, repo: str, base_sha: str, issue_number: int) -> tuple[dict, dict]:
    """Check source, file whitelist, exact hashes and original verification lineage."""
    validate_source(source)
    if source["repository"].lower() != os.environ.get("GITHUB_REPOSITORY", "xixi-box/oss-scout").lower():
        raise ValueError("retained delivery source is outside the controller repository")
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("retained delivery directory is unsafe")
    root = directory.resolve()
    manifest_path = root / "manifest.json"
    if not _regular_file(manifest_path, root):
        raise ValueError("retained delivery manifest is unsafe")
    manifest = json.loads(manifest_path.read_bytes())
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), dict):
        raise ValueError("retained delivery manifest is invalid")
    old_job = manifest.get("job") or {}
    publication = manifest.get("publication") or {}
    if (not isinstance(old_job, dict) or not isinstance(publication, dict)
            or old_job.get("job_id") != source["job_id"]
            or old_job.get("repo", "").lower() != repo.lower()
            or old_job.get("base_sha") != base_sha
            or publication.get("upstream", "").lower() != repo.lower()
            or publication.get("issue_number") != issue_number
            or manifest.get("source_repository", "").lower() != source["repository"].lower()
            or (manifest.get("source_run") is not None
                and str(manifest["source_run"]) != str(source["run_id"]))
            or manifest.get("patch_sha256") != source["patch_sha256"]
            or not set(manifest["files"]) <= set(_BUNDLE_FILES)
            or set(manifest["files"]) | {"manifest.json"} != {path.name for path in root.iterdir()}):
        raise ValueError("retained delivery source, target, base or manifest differs")
    files = {}
    for name, digest in manifest["files"].items():
        path = root / name
        if not _regular_file(path, root) or path.stat().st_size > 8_000_000:
            raise ValueError("retained delivery file is unsafe or too large")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("retained delivery file hash differs")
        files[name] = raw
    if _verified_patch_digest(old_job, files) != source["patch_sha256"]:
        raise ValueError("retained delivery patch differs")
    return files, manifest


def publication_retry_fields(root: Path, entry: dict, publication: dict, *, job_id: str,
                             repo: str, issue_number: int, run_id: str, repository: str,
                             now) -> dict:
    """Only completed, confirmed-unpublished jobs may request a new recovery job.

    The caller has already reconciled the remote head and released its original
    reservation. A bounded retry is not permission to repeat the previous write.
    """
    kind = publication.get("failure_kind")
    if kind not in ("publication_rate_limited", "review_communication_unavailable",
                    "review_evidence_changed", "publication_evidence_unavailable"):
        return {}
    if publication.get("ok") or publication.get("created") or publication.get("pr"):
        return {}
    if entry.get("last_publication_retry_job_id") == job_id:
        # Reconciliation can repeat after a crash before finishing the ledger.
        # Preserve the first backoff and bounded retry count for this exact run.
        return {"repair_outcome": "verified_publication_retry",
                "abort_reason": entry.get("abort_reason") or "已验证修复待恢复"}
    failures = int(entry.get("publication_retry_count") or 0)
    if failures >= 3:
        return {}
    folder = root / "artifacts/verified-deliveries" / job_id
    try:
        manifest = json.loads((folder / "manifest.json").read_bytes())
        base = manifest["job"]["base_sha"]
        source = {"job_id": job_id, "run_id": str(run_id), "repository": repository,
                  "patch_sha256": manifest["patch_sha256"], "base_sha": base}
        load_bundle(folder, source, repo=repo, base_sha=base, issue_number=issue_number)
    except (OSError, ValueError, TypeError, KeyError):
        return {}
    import datetime as dt
    delay = 3600 if kind == "publication_rate_limited" else min(3600, 300 * 2 ** failures)
    due = (now + dt.timedelta(seconds=delay)).isoformat()
    return {"verified_delivery_source": source, "publication_retry_count": failures + 1,
            "publication_retry_kind": kind, "retry_authorized_job_id": job_id,
            "last_publication_retry_job_id": job_id,
            "repair_outcome": "verified_publication_retry", "eligible_at": due,
            "screen_retry_at": due, "scheduled_consume_day": now.date().isoformat(),
            "staged_at": entry.get("staged_at") or now.isoformat(),
            "abort_reason": "已验证修复待恢复 · " + str(publication.get("error") or kind)[:230]}


def recovery_producer(job, work: Path, _context: str) -> dict:
    """Reuse an exact retained patch; the agent owns tests and self-checks."""
    directory = os.environ.get("OSSCOUT_VERIFIED_DELIVERY_DIR", "")
    if not directory:
        raise RuntimeError("retained_delivery_unavailable: no imported verified bundle")
    try:
        files, manifest = load_bundle(Path(directory), job.verified_delivery_source,
            repo=job.repo, base_sha=job.base_sha, issue_number=job.publish.get("issue_number"))
        report = json.loads(files["test-report.json"])
        return {"patch": files["change.patch"].decode("utf-8"), "verification_mode": "agent",
                "agent_report": report.get("agent_report") or report,
                "test_cmd": report.get("test_cmd"), "root_cause": report.get("root_cause") or "",
                "coverage": report.get("coverage") or {}, "pr_title": report.get("pr_title") or "",
                "pr_body": report.get("pr_body") or "", "notes": [
                    "retained agent delivery reused under a new controller reservation"],
                "delivery_preflight": {"recovered_from": manifest["job"]["job_id"],
                                       "source_patch_sha256": manifest["patch_sha256"]}}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError("retained_delivery_invalid: " + str(exc)[:300]) from None


def download_source(job: dict, destination: Path) -> None:
    """Only download an exact allowlisted artifact from this controller repository."""
    source = job.get("verified_delivery_source")
    if not source:
        return
    validate_source(source)
    if source["repository"].lower() != os.environ.get("GITHUB_REPOSITORY", "").lower():
        raise ValueError("retained delivery source is outside the controller repository")
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    response = subprocess.run(["gh", "run", "download", str(source["run_id"]),
        "--repo", source["repository"], "--name", "oss-verified-" + source["job_id"],
        "--dir", str(destination)], capture_output=True, timeout=120)
    if response.returncode:
        raise RuntimeError("retained delivery download failed; no publication attempted")
    load_bundle(destination, source, repo=job["repo"], base_sha=job["base_sha"],
                issue_number=job["publish"]["issue_number"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--out")
    args = parser.parse_args()
    job = json.loads(Path(args.job).read_text(encoding="utf-8"))
    ready = True
    try:
        download_source(job, Path(args.destination))
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
        from ..schemas import Result
        from .core import emit_envelope
        ready = False
        out = Path(args.out) if args.out else Path(args.destination).parent / "artifacts"
        out.mkdir(parents=True, exist_ok=True)
        # Subprocess exception text can contain environment/arguments. Keep only
        # our own fixed diagnostics and exception class, never gh response bodies.
        detail = (str(exc) if type(exc) in (RuntimeError, ValueError) else type(exc).__name__)[:300]
        result = Result(job_id=job.get("job_id", ""), backend="github_actions", outcome="failed",
                        base_sha=job.get("base_sha", ""),
                        unverified_items=["retained_delivery_unavailable: " + detail])
        (out / "result.json").write_text(json.dumps(result.to_dict()), encoding="utf-8")
        emit_envelope(result, out)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write("ready=" + str(ready).lower() + "\n")


if __name__ == "__main__":
    main()
