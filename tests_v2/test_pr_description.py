import json

from scout_v2.workers import publish_remote, repair


def test_agent_is_asked_to_use_final_diff_and_actual_repo_template():
    prompt = repair.FIX_PROMPT.format(context="issue", rules="rules")
    assert "final diff" in prompt and "PULL_REQUEST_TEMPLATE" in prompt
    assert "{VERIFIED_TESTS}" in prompt


def test_description_template_inserts_verified_results_and_keeps_trace(tmp_path):
    report = {"root_cause": "Null input was dereferenced.", "test_cmd": ["pytest", "test_null.py", "-v"],
              "regression_test_files": ["test_null.py"], "additional_checks": {"lint": 0, "failed-check": 1},
              "pr_body": "## Summary\nHandle null input before dereference.\n\n## Test plan\n{VERIFIED_TESTS}\n\n- [ ] Manual UI check"}
    (tmp_path / "test-report.json").write_text(json.dumps(report))
    cfg = {"issue_number": 42, "body": "Fixes #42\nAI assistance: Codex\n<!-- oss-scout-job:job-42 -->"}
    body = publish_remote._rewrite_body(cfg, tmp_path, repair_agent="codex")
    assert "Handle null input" in body and "## Test plan" in body
    assert "pytest test_null.py -v" in body and "fails on base; passes with this patch" in body
    assert "Additional check passed: `lint`" in body and "failed-check" not in body
    assert "- [ ] Manual UI check" in body and "<!-- oss-scout-job:job-42 -->" in body
    assert "Fixes #42" in body and "AI assistance:" not in body


def test_old_jobs_and_invalid_drafts_fall_back_to_facts_without_disclaimer(tmp_path):
    for draft in (None, "All tests passed without evidence.", "AI assistance: Codex\n{VERIFIED_TESTS}"):
        (tmp_path / "test-report.json").write_text(json.dumps({
            "root_cause": "Null input was dereferenced.", "test_cmd": ["pytest", "test_null.py"], "pr_body": draft}))
        body = publish_remote._rewrite_body({"issue_number": 42}, tmp_path)
        assert "Root cause: Null input" in body and "Tests:" in body
        assert "All tests passed" not in body and "AI assistance:" not in body


def test_oversized_generated_text_is_not_cut_mid_title_or_template(tmp_path):
    (tmp_path / repair.RESULT_FILE).write_text(json.dumps({
        "pr_title": "A" * 241, "pr_body": "{VERIFIED_TESTS}" + "B" * 16000,
        "root_cause": "A real cause."}))
    result = repair._read_result_file(tmp_path)
    assert "pr_title" not in result and "pr_body" not in result
    assert result["root_cause"] == "A real cause."
