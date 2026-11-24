import json
from pathlib import Path

import pytest

from scout_v2.workers import publish_remote, repair
from scout_v2.workers.delivery_materials import COMPLETION_TASK


def test_agent_is_asked_to_use_final_diff_and_actual_repo_template():
    prompt = repair.FIX_PROMPT.format(context="issue", rules="rules")
    assert "final diff" in prompt and "PULL_REQUEST_TEMPLATE" in prompt
    assert "{VERIFIED_TESTS}" in prompt


def test_description_template_inserts_verified_results_and_keeps_trace(tmp_path):
    report = {"root_cause": "Null input was dereferenced.", "test_cmd": ["pytest", "test_null.py", "-v"],
              "regression_test_files": ["test_null.py"], "additional_checks": {"lint": 0, "failed-check": 1},
              "pr_body": "## Summary\nHandle null input before dereference.\n\n## Test plan\n{VERIFIED_TESTS}"
                         "\n\n- [ ] Manual UI check\n- [ ] A human has read and takes responsibility for this PR."}
    (tmp_path / "test-report.json").write_text(json.dumps(report))
    cfg = {"issue_number": 42, "body": "Fixes #42\nAI assistance: Codex\n<!-- oss-scout-job:job-42 -->"}
    body = publish_remote._rewrite_body(cfg, tmp_path, repair_agent="codex")
    assert "Handle null input" in body and "## Test plan" in body
    assert "pytest test_null.py -v" in body and "fails on base; passes with this patch" in body
    assert "Additional check passed: `lint`" in body and "failed-check" not in body
    assert "- [ ] Manual UI check" in body and "<!-- oss-scout-job:job-42 -->" in body
    assert "- [ ] A human has read and takes responsibility" in body
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


def test_agent_reported_results_are_rendered_without_inventing_verification(tmp_path):
    report = {"verification_mode": "agent", "test_cmd": ["pytest", "test_null.py"],
              "agent_report": {"test_results": ["4 tests passed.", "No base comparison was run."]},
              "pr_body": "## Tests\n{VERIFIED_TESTS}"}
    (tmp_path / "test-report.json").write_text(json.dumps(report))
    body = publish_remote._rewrite_body({"issue_number": 42}, tmp_path)
    assert "Tests: `pytest test_null.py`." in body
    assert "Result: 4 tests passed." in body
    assert "Result: No base comparison was run." in body
    assert "fails on base" not in body and "passes with this patch" not in body
    assert "Agent-reported" not in body and "AI assistance" not in body and "oss-scout" not in body


@pytest.mark.parametrize("repo", ["qdrant/qdrant", "kgateway-dev/kgateway", "agentgateway/agentgateway", "bytedance/deer-flow"])
def test_shared_prompt_leaves_template_attestations_unchecked_and_allows_handoff(repo):
    root = Path(__file__).resolve().parents[1]
    prompt = repair.FIX_PROMPT.format(context="issue", rules=repair.repo_rules_block(repo, root))
    assert "For every repository, draft the PR motivation and body yourself" in prompt
    assert "deliver the normal publishable handoff" in prompt.lower()
    assert "leave unverified boxes unchecked" in prompt
    assert "set publication_ready=true if the attestation" in prompt
    assert "prior\nmaintainer approval" in prompt and 'status="blocked_publication"' in prompt
    assert "deliver the normal publishable" in COMPLETION_TASK
    assert "leave unverified human attestations unchecked" in COMPLETION_TASK


@pytest.mark.parametrize(("agent", "name"), [("codex", "Codex"), ("opencode", "OpenCode")])
def test_qdrant_discloses_agent_written_motivation_and_keeps_test_report(tmp_path, agent, name):
    report = {"verification_mode": "agent", "test_cmd": ["cargo", "test", "regression"],
              "pr_body": "## Motivation\nPreserve the expected result for empty inputs.\n{VERIFIED_TESTS}"}
    (tmp_path / "test-report.json").write_text(json.dumps(report), encoding="utf-8")
    body = publish_remote._rewrite_body({"upstream": "qdrant/qdrant", "issue_number": 42}, tmp_path, repair_agent=agent)
    assert "Fixes #42" in body and "## Motivation" in body and "cargo test regression" in body
    assert f"{name} drafted the PR motivation and description" in body
