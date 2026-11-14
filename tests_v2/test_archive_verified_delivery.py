import base64
import io
import json
import zipfile

from scripts import archive_verified_delivery as archive


def test_archive_selects_original_result_when_bundle_is_nested(tmp_path, monkeypatch):
    job = {"job_id": "job-archive", "base_sha": "a" * 40, "repo": "up/repo", "mode": "repair"}
    source = tmp_path / "source-123"
    original = source / "oss-result-job-archive"
    nested = original / "verified-delivery"
    nested.mkdir(parents=True)
    result = {"job_id": job["job_id"], "base_sha": job["base_sha"]}
    (original / "result.json").write_text(json.dumps(result))
    (nested / "result.json").write_text(json.dumps(result))
    log = io.BytesIO()
    with zipfile.ZipFile(log, "w") as stream:
        stream.writestr("job.txt", "OSS_JOB_B64: " + base64.b64encode(json.dumps(job).encode()).decode())
    calls = []

    def gh(*args, binary=False):
        calls.append(args)
        if args[0] == "run":
            assert args[1] == "download"
            return ""
        if args[1].endswith("/logs"):
            return log.getvalue()
        return json.dumps({"status": "completed", "path": ".github/workflows/oss-scout-repair.yml",
                           "display_title": "repair job-archive"})

    def preserve(payload, out, publication):
        assert out == original and payload["source_run"] == "123"
        assert payload["base_sha"] == job["base_sha"]
        return {"ok": True, "path": str(nested), "patch_sha256": "verified"}

    monkeypatch.setattr(archive, "gh", gh)
    monkeypatch.setattr(archive, "preserve_verified_bundle", preserve)
    assert archive.archive_run("123", tmp_path) == nested
    assert all(args[0] in ("run", "api") for args in calls)
