"""Check agent handoff completeness, never substitute it for independent replay."""
from __future__ import annotations

from pathlib import Path, PurePosixPath
import os
import re


def redact_log(text: str) -> str:
    secrets = {value for name, value in os.environ.items()
               if re.search(r"TOKEN|SECRET|KEY|PASSWORD|AUTH", name, re.I) and len(value) >= 4}
    for value in sorted(secrets, key=len, reverse=True):
        text = text.replace(value, "[REDACTED]")
    text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9_.~+/-]+=*", "Bearer [REDACTED]", text)
    return re.sub(r'''(?i)(["']?(?:access_token|refresh_token|id_token|api_key)["']?\s*[:=]\s*["']?)[^\s,"'}]+''',
                  r"\1[REDACTED]", text)


def _source_file(work: Path, value) -> bool:
    if (not isinstance(value, str) or not value or len(value) > 4096
            or "\\" in value or ":" in value or "\x00" in value):
        return False
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("..", ".git") for part in path.parts):
        return False
    full = work / value
    try:
        return (full.resolve().is_relative_to(work.resolve()) and full.is_file()
                and not any(part.is_symlink() for part in (full, *full.parents) if part != work.parent))
    except (OSError, ValueError, RuntimeError):
        return False


def coverage_issues(coverage, work: Path, markers: dict) -> list[str]:
    if not isinstance(coverage, dict):
        return ["coverage: missing real entrypoint, call_chain, regression_case, scenarios and limitations"]
    errors = []
    entry = coverage.get("entrypoint")
    chain = coverage.get("call_chain")
    if not isinstance(chain, list) or not 1 <= len(chain) <= 12:
        errors.append("coverage.call_chain: provide 1-12 actual source path/symbol references")
        chain = []
    for ref in [entry, *chain]:
        if (not isinstance(ref, dict) or not _source_file(work, ref.get("path"))
                or not isinstance(ref.get("symbol"), str) or not ref["symbol"].strip()):
            errors.append("coverage: each entrypoint/call_chain reference needs an existing safe source path and symbol")
            break
    if entry not in chain:
        errors.append("coverage.call_chain: include the real entrypoint")
    expected_case = ((markers.get("test_report") or {}).get("case")
                     if isinstance(markers.get("test_report"), dict) else None)
    failures = markers.get("base_failure")
    expected_case = expected_case or (failures[0] if isinstance(failures, list) and failures else None)
    if not expected_case or coverage.get("regression_case") != expected_case:
        errors.append("coverage.regression_case: must match the declared regression case")
    for field, minimum in (("scenarios", 1), ("limitations", 0)):
        values = coverage.get(field)
        if (not isinstance(values, list) or not minimum <= len(values) <= 20
                or any(not isinstance(v, str) or not v.strip() for v in values)):
            errors.append(f"coverage.{field}: provide a list of observed scenarios or explicitly untested paths")
    return errors


def material_issues(markers: dict, work: Path) -> list[str]:
    from .repair import _verification_plan_valid

    issues = []
    for field in ("test_cmd", "root_cause", "pr_title", "pr_body"):
        if not isinstance(markers.get(field), str) or not markers[field].strip():
            issues.append(f"{field}: missing")
    if not _source_file(work, markers.get("test_file")):
        issues.append("test_file: must identify an existing safe regression file")
    if not _verification_plan_valid(markers):
        issues.append("verification_plan: need setup_steps, actual base failure case/assertion, patched success or valid JUnit specification")
    if str(markers.get("pr_body") or "").count("{VERIFIED_TESTS}") != 1:
        issues.append("pr_body: include exactly one {VERIFIED_TESTS} placeholder")
    issues.extend(coverage_issues(markers.get("coverage"), work, markers))
    return issues


def reported_blocker(markers: dict) -> str:
    """An explicit inability to reproduce is a blocker, not a missing handoff.

    Do not infer blockers from free text or a missing field. This only preserves
    the agent's explicit structured refusal, and never makes a delivery pass.
    """
    status = markers.get("status")
    if (not isinstance(status, str) or not re.fullmatch(r"blocked(?:_[a-z_]+)?|infeasible", status)
            or not (markers.get("publication_ready") is False or markers.get("publication_allowed") is False)):
        return ""
    reason = markers.get("blocker") or markers.get("blocked_reason") or markers.get("blockers")
    if isinstance(reason, list) and reason and all(isinstance(item, str) for item in reason):
        reason = "; ".join(reason[:20])
    return reason.strip()[:1500] if isinstance(reason, str) else ""


COMPLETION_TASK = """Finish the existing repair handoff in OSSCOUT_RESULT.json.
Read OSSCOUT_TASK.md and OSSCOUT_DELIVERY_CHECK.json first. Keep all existing
publication and verification requirements. This is one bounded completion pass,
not permission to invent evidence or weaken tests to get a PR opened.

Inspect the existing real command logs, OSSCOUT_BASE.log, OSSCOUT_PATCHED.log and
the sanitized CLI event log when available. Recover missing fields only from
actual observations. If observations are absent or contradict the report, rerun
the necessary commands on the correct original/patched checkout, restoring the
full final patch afterwards. Never label dependency/import/build/collection
errors as the target assertion. Do not hand-write machine reports or success logs.

Trace the real production entrypoint through parsing, serialization/copying,
adapters and sibling consumers to the fix. Build the regression through that
entrypoint or a faithful existing harness. A fabricated middle object must not
bypass the step that loses or transforms data. Cover the observed failing path,
the relevant sibling/boundary behavior, and state what was NOT exercised.
For test-only repairs, keep behavioral assertions and justify fixture/timing edits.

Write complete coverage references and test observations into OSSCOUT_RESULT.json.
If a prerequisite, private disclosure rule, unavailable dependency or irreducible
scope prevents a truthful handoff, report that blocker; do not claim completion.
Preserve the existing JSON object and update its fields in place. Keep the schema
in OSSCOUT_TASK.md: setup_steps/test_cmd/test_file/base_failure/patched_success/
test_report/coverage/pr_title/pr_body are top-level fields, not a nested report.
If the task is genuinely blocked, use status="blocked", publication_ready=false,
and a concrete blocker with observed evidence; do not invent the missing fields.
"""
