"""Read independently generated test reports; never trust an agent's pass claim."""
from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path, PurePosixPath
import xml.etree.ElementTree as ET


def report_spec(plan: dict | None) -> dict | None:
    spec = (plan or {}).get("test_report")
    if spec is None:
        return None
    if not isinstance(spec, dict) or spec.get("format") != "junit":
        raise ValueError("test_report format must be junit")
    path = spec.get("path")
    case = spec.get("case")
    if (not isinstance(path, str) or not path or "\\" in path or ":" in path
            or PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts
            or ".git" in PurePosixPath(path).parts
            or not isinstance(case, str) or not case.strip()):
        raise ValueError("test_report needs a safe relative path and exact case identity")
    return {"format": "junit", "path": str(PurePosixPath(path)), "case": case.strip()}


def _report_path(work: Path, spec: dict) -> Path:
    path = work / spec["path"]
    if not path.resolve().is_relative_to(work.resolve()):
        raise ValueError("test report escapes the verification checkout")
    if any(part.is_symlink() for part in [path, *path.parents] if part != work.parent):
        raise ValueError("test report must not use symlinks")
    return path


def prepare_report(work: Path, plan: dict | None) -> None:
    spec = report_spec(plan)
    if spec:
        path = _report_path(work, spec)
        tracked = subprocess.run(["git", "ls-files", "--error-unmatch", "--", spec["path"]],
                                 cwd=work, capture_output=True, timeout=15)
        if tracked.returncode == 0:
            raise ValueError("test report path must not overwrite a tracked file")
        if path.exists():
            if not path.is_file():
                raise ValueError("test report path is not a file")
            path.unlink()  # A stale report must never satisfy a new test run.


def collect_report(work: Path, out_dir: Path, plan: dict, stage: str) -> dict:
    spec = report_spec(plan)
    if spec is None:
        return {"format": "log"}
    path = _report_path(work, spec)
    if not path.is_file() or path.stat().st_size > 5_000_000:
        raise ValueError("JUnit report missing or too large")
    raw = path.read_bytes()
    if re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", raw, re.I):
        raise ValueError("JUnit declarations/entities are not supported")
    try:
        tree = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError("JUnit report is invalid XML") from exc
    for node in tree.iter():
        node.tag = node.tag.rsplit("}", 1)[-1]  # Accept namespaced JUnit writers too.
    rows = []
    identities = set()
    for case in tree.iter("testcase"):
        name = case.get("name", "")
        identity = "::".join(filter(None, [case.get("classname", ""), name]))
        if not name or identity in identities:
            raise ValueError("JUnit report contains missing or duplicate case identities")
        identities.add(identity)
        status = "passed"
        if (case.get("status", "").lower() in ("notrun", "disabled", "skipped")
                or case.get("result", "").lower() in ("suppressed", "skipped")):
            status = "skipped"
        messages = []
        for kind in ("failure", "error", "skipped"):
            elements = case.findall(kind)
            if elements:
                if status != "passed" and not (kind == "skipped" and status == "skipped"):
                    raise ValueError("JUnit case has conflicting outcomes")
                status = {"failure": "failed", "error": "error", "skipped": "skipped"}[kind]
                messages = [element.get("message", "") + "\n" + "".join(element.itertext())
                            for element in elements]
        rows.append({"id": identity, "name": name, "status": status,
                     "message": "\n".join(messages)[:20000]})
    for suite in [node for node in tree.iter() if node.tag in ("testsuites", "testsuite")]:
        cases = list(suite.iter("testcase"))
        statuses = []
        for node in cases:
            statuses.append("error" if node.find("error") is not None else
                            "failed" if node.find("failure") is not None else
                            "skipped" if (node.find("skipped") is not None
                                          or node.get("status", "").lower() in ("notrun", "disabled", "skipped")
                                          or node.get("result", "").lower() in ("suppressed", "skipped")) else "passed")
        for key, expected in (("tests", len(cases)), ("errors", statuses.count("error")),
                              ("failures", statuses.count("failed")), ("skipped", statuses.count("skipped"))):
            if key in suite.attrib and (not suite.get(key, "").isdigit() or int(suite.get(key)) != expected):
                raise ValueError("JUnit suite counts do not match its testcase records")
    selected = [row for row in rows if spec["case"] in (row["id"], row["name"])]
    if len(selected) != 1:
        raise ValueError("JUnit target case missing or ambiguous")
    artifact = f"{stage}-regression.junit.xml"
    (out_dir / artifact).write_bytes(raw)
    return {"format": "junit", "case": selected[0], "total": len(rows),
            "failures": sum(row["status"] == "failed" for row in rows),
            "errors": sum(row["status"] == "error" for row in rows),
            "artifact": artifact, "sha256": hashlib.sha256(raw).hexdigest()}


def check_report(evidence: dict, plan: dict, stage: str, base: dict | None = None) -> None:
    case = evidence["case"]
    if stage == "base":
        if case["status"] != "failed" or evidence["errors"]:
            raise ValueError("JUnit base must fail by a test assertion, without runner errors")
        # Match the reported bug symptom against the actual failure payload.
        from .repair import _evidence_matches, _test_case_identity, _verification_error
        if _test_case_identity(plan["base_failure"][0]) not in (case["id"], case["name"]):
            raise ValueError("JUnit target identity does not match the base regression plan")
        if _verification_error(1, case["message"], ""):
            raise ValueError("JUnit base failure is an environment/build error, not bug reproduction")
        if not _evidence_matches(plan["base_failure"][1:], case["message"], ""):
            raise ValueError("JUnit base assertion does not demonstrate the target bug")
    elif (not base or case["id"] != base["case"]["id"] or case["status"] != "passed"
          or evidence["failures"] or evidence["errors"]):
        raise ValueError("JUnit patched run must pass the identical base regression case")
