"""Retain an existing repair's verified files. This command never publishes."""
from __future__ import annotations

import argparse
import base64
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scout_v2.workers.publication_guard import preserve_verified_bundle


def gh(*args, binary=False):
    result = subprocess.run(["gh", *args], capture_output=True, timeout=180, check=True)
    return result.stdout if binary else result.stdout.decode("utf-8")


def archive_run(run_id: str, destination: Path, repository="xixi-box/oss-scout") -> Path:
    if not re.fullmatch(r"[1-9]\d*", run_id):
        raise ValueError("source run must be a positive Actions run ID")
    run = json.loads(gh("api", f"repos/{repository}/actions/runs/{run_id}"))
    if run.get("status") != "completed" or run.get("path", "").split("@")[0] != ".github/workflows/oss-scout-repair.yml":
        raise ValueError("only completed repair workflows can be archived")
    source = destination / ("source-" + run_id)
    source.mkdir(parents=True, exist_ok=True)
    gh("run", "download", run_id, "--repo", repository, "--pattern", "oss-result-*", "--dir", str(source))
    results = [path for path in source.rglob("result.json")
               if "verified-delivery" not in path.relative_to(source).parts]
    if len(results) != 1:
        raise ValueError("repair result missing or ambiguous")
    out = results[0].parent
    result = json.loads(results[0].read_text(encoding="utf-8"))
    if run.get("display_title") != "repair " + result.get("job_id", ""):
        raise ValueError("source run does not match repair identity")
    raw = gh("api", f"repos/{repository}/actions/runs/{run_id}/logs", binary=True)
    with zipfile.ZipFile(io.BytesIO(raw)) as logs:
        text = "\n".join(logs.read(name).decode("utf-8", "replace") for name in logs.namelist() if name.endswith(".txt"))
    jobs = []
    for token in set(re.findall(r"[A-Za-z0-9+/]{120,}={0,2}", text)):
        try:
            job = json.loads(base64.b64decode(token))
        except (ValueError, UnicodeError):
            continue
        if (isinstance(job, dict) and job.get("job_id") == result["job_id"]
                and job.get("base_sha") == result.get("base_sha") and job.get("mode") == "repair"):
            jobs.append(job)
    if len(jobs) != 1:
        raise ValueError("source job payload missing or ambiguous")
    job = {**jobs[0], "source_run": run_id, "runner_repo": repository}
    publication = json.loads((out / "publish.json").read_text(encoding="utf-8")) if (out / "publish.json").exists() else {"ok": False}
    saved = preserve_verified_bundle(job, out, publication)
    if not saved.get("ok"):
        raise ValueError(saved.get("error"))
    print(json.dumps({"source_run": run_id, "job_id": job["job_id"],
                      "patch_sha256": saved["patch_sha256"], "saved": True}))
    return Path(saved["path"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--repository", default="xixi-box/oss-scout")
    arguments = parser.parse_args()
    archive_run(arguments.run, arguments.out, arguments.repository)
