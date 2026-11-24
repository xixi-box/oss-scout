"""Early policy/dependency screening using the two real Nov 15 failures."""
import base64
import copy
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from dashboard import data
from scout_v2 import screening_evidence as evidence
from scout_v2.workers.openjev import OpenJevClient


CASES = json.loads((Path(__file__).parent / "fixtures/nov15_screening_cases.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    evidence._CACHE.clear()
    monkeypatch.setenv("OSSCOUT_DASHBOARD_ROOT", str(tmp_path))
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    yield
    evidence._CACHE.clear()


def item(name):
    fixture = CASES[name]
    return {"repo": fixture["repo"], "number": fixture["number"], "title": fixture["title"],
            "body_excerpt": fixture["body"], "reporter": fixture["reporter"],
            "reporter_association": fixture["author_association"], "reporter_type": fixture["reporter_type"],
            "issue_comments": copy.deepcopy(fixture["comments"]), "labels": []}


def policy_reads(monkeypatch, repo, *, security=None, contributing="Keep changes focused.", merged=False):
    calls = []

    def get(path, *, optional=False):
        calls.append(path)
        if path == f"repos/{repo}":
            return {"default_branch": "main"}
        if path == f"repos/{repo}/commits/main":
            return {"sha": "a" * 40}
        if path.startswith(f"repos/{repo}/contents/"):
            filename = path.split("/contents/", 1)[1].split("?", 1)[0]
            content = {"SECURITY.md": security, "CONTRIBUTING.md": contributing}.get(filename)
            if content is not None:
                raw = content.encode()
                return {"type": "file", "encoding": "base64", "sha": "b" * 40,
                        "size": len(raw), "content": base64.b64encode(raw).decode()}
        if path.lower() == f"repos/{repo.lower()}/pulls/16598":
            return {"number": 16598, "state": "closed" if merged else "open",
                    "merged_at": "2026-11-15T00:00:00Z" if merged else None,
                    "html_url": f"https://github.com/{repo}/pull/16598", "base": {"ref": "main"},
                    "head": {"sha": "c" * 40}}
        if optional:
            return None
        raise evidence.ScreeningEvidenceUnavailable("test dependency read unavailable")

    monkeypatch.setattr(evidence, "_get", get)
    return calls


def model_response(monkeypatch, *, private=0.0, pending=0.0, unknown=0.0, eligible=0.9, missing=None):
    calls = []

    def respond(request, **kwargs):
        calls.append(json.loads(request.data))
        answers = {"eligible": {"noul": eligible}, "private_disclosure": {"noul": private},
                   "unmet_prerequisite": {"noul": pending}, "prerequisite_unknown": {"noul": unknown}}
        answers.pop(missing, None)
        return io.BytesIO(json.dumps({"answers": answers}).encode())

    monkeypatch.setattr("scout_v2.workers.openjev.urllib.request.urlopen", respond)
    return calls


def test_real_dify_vulnerability_is_screened_with_full_private_disclosure_policy(monkeypatch):
    fixture = CASES["dify_private_disclosure"]
    candidate = item("dify_private_disclosure")
    policy_reads(monkeypatch, candidate["repo"], security=fixture["security_policy"],
                 contributing=fixture["contribution_excerpt"])
    requests = model_response(monkeypatch, private=0.99, eligible=0.01)
    result = data.fine_screen_issue(SimpleNamespace(), candidate, lane="open")
    assert result["usable"] is False and result["blocking_gate"] == "private_disclosure"
    assert "私报" in result["reason"] and "SECURITY.md" in result["reason"]
    assert fixture["security_policy"] in requests[0]["state"]["repo_rules"]
    assert candidate["body_excerpt"] == requests[0]["state"]["body"]
    assert result["screening_context_version"] == 2


@pytest.mark.parametrize("merged", [False, True])
def test_real_qwen_collaborator_coordination_uses_current_pr_status(monkeypatch, merged):
    candidate = item("qwen_prerequisite")
    policy_reads(monkeypatch, candidate["repo"], merged=merged)
    requests = model_response(monkeypatch, pending=0.0 if merged else 0.99)
    result = data.fine_screen_issue(SimpleNamespace(), candidate, lane="open")
    assert result["usable"] is merged
    state = requests[0]["state"]
    assert state["comments"][0]["association"] == "COLLABORATOR"
    assert state["comments"][0]["author"] == "qwen-code-review-bot"
    assert "落定之后" in state["comments"][0]["body"]
    facts = state["prerequisite_pull_requests"]
    assert len(facts) == 1 and facts[0]["number"] == 16598 and facts[0]["merged"] is merged
    assert facts[0]["mentions"][0]["association"] == "COLLABORATOR"
    if not merged:
        assert "16598" in result["reason"] and "open" in result["reason"]


@pytest.mark.parametrize("body", [
    "Login returns an unexpected 403 for an authorized owner; no privilege bypass is involved.",
    "Fix the spelling of the security documentation heading.",
    "Authentication tests fail because the test clock is not isolated.",
    "Related discussion #16598 covers a different feature.",
    "Previously we needed #16598, but that requirement is withdrawn; this can land independently.",
])
def test_terms_links_and_withdrawn_requirements_do_not_hard_filter(monkeypatch, body):
    candidate = item("qwen_prerequisite")
    candidate.update(body_excerpt=body, issue_comments=[])
    policy_reads(monkeypatch, candidate["repo"])
    model_response(monkeypatch)
    assert data.fine_screen_issue(SimpleNamespace(), candidate, lane="open")["usable"] is True


@pytest.mark.parametrize("text", ["Do not implement until `#16598` has merged.", "需先合并 #16598，然后再处理。"])
def test_reference_extraction_is_bilingual_and_preserves_inline_identifiers(monkeypatch, text):
    candidate = item("qwen_prerequisite")
    candidate.update(body_excerpt=text, issue_comments=[])
    policy_reads(monkeypatch, candidate["repo"])
    assert evidence.prerequisite_facts(candidate)[0]["number"] == 16598


def test_bare_pr_mention_does_not_trigger_dependency_network_requests(monkeypatch):
    candidate = item("qwen_prerequisite")
    candidate.update(body_excerpt="Related PR #16598 has useful background.", issue_comments=[])
    monkeypatch.setattr(evidence, "_get", lambda *a, **kw: pytest.fail("not a stated prerequisite"))
    assert evidence.prerequisite_facts(candidate) == []


def test_docling_discussion_reference_is_not_looked_up_as_an_issue(monkeypatch):
    candidate = item("qwen_prerequisite")
    candidate.update(repo="docling-project/docling", number=3671, body_excerpt="", issue_comments=[{
        "body": "Measured on Windows 11 while debugging discussion #3829 (same error signature): "
                "the conversion succeeds after the upgrade.",
    }])
    monkeypatch.setattr(evidence, "_reference_state", lambda *args: pytest.fail("discussion is not an issue"))
    assert evidence.prerequisite_facts(candidate) == []


def test_policy_fetch_error_is_retryable_and_never_calls_ai(monkeypatch):
    monkeypatch.setattr(evidence, "_get", lambda *a, **kw: (_ for _ in ()).throw(
        evidence.ScreeningEvidenceUnavailable("policy unavailable")))
    monkeypatch.setattr(OpenJevClient, "score_issue", lambda *a: pytest.fail("must not triage without policy"))
    verdict = data.fine_screen_issue(SimpleNamespace(), item("dify_private_disclosure"), lane="open")
    assert verdict["usable"] is None and "policy unavailable" in verdict["reason"]


def test_dependency_fetch_error_is_retryable_not_formal_rejection(monkeypatch):
    candidate = item("qwen_prerequisite")
    monkeypatch.setattr(data, "repo_rules_text", lambda _: "complete cached repository policy")
    monkeypatch.setattr(evidence, "_get", lambda *a, **kw: (_ for _ in ()).throw(
        evidence.ScreeningEvidenceUnavailable("dependency unavailable")))
    monkeypatch.setattr(OpenJevClient, "score_issue", lambda *a: pytest.fail("dependency state missing"))
    verdict = data.fine_screen_issue(SimpleNamespace(), candidate, lane="open")
    assert verdict["usable"] is None and "dependency unavailable" in verdict["reason"]


@pytest.mark.parametrize("status", [403, 429, 500])
def test_optional_policy_is_only_absent_on_confirmed_404(monkeypatch, status):
    monkeypatch.setattr(evidence.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=1, stdout=json.dumps({"status": str(status), "message": "not available"})))
    with pytest.raises(evidence.ScreeningEvidenceUnavailable):
        evidence._get("repos/example/repo/contents/SECURITY.md", optional=True)


def test_confirmed_404_is_distinct_from_failed_fetch(monkeypatch):
    monkeypatch.setattr(evidence.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=1, stdout='{"status":"404"}'))
    assert evidence._get("repos/example/repo/contents/SECURITY.md", optional=True) is None


def test_policy_cache_retains_full_documents_without_refetching(monkeypatch):
    calls = policy_reads(monkeypatch, "example/repo", security="Private reporting policy\n" * 800)
    first = evidence.render_repository_policy("example/repo")
    count = len(calls)
    assert evidence.render_repository_policy("example/repo") == first
    assert len(calls) == count and first.count("Private reporting policy") == 800


def test_policy_failure_cache_does_not_become_an_empty_success(monkeypatch):
    calls = []
    monkeypatch.setattr(evidence, "_get", lambda *a, **kw: calls.append(a) or (_ for _ in ()).throw(
        evidence.ScreeningEvidenceUnavailable("unavailable")))
    for _ in range(2):
        with pytest.raises(evidence.ScreeningEvidenceUnavailable):
            evidence.repository_policy_documents("example/repo")
    assert len(calls) == 1


def test_context_fingerprint_tracks_policy_comments_and_dependency_state(monkeypatch):
    candidate = item("qwen_prerequisite")
    policy_reads(monkeypatch, candidate["repo"])
    first = evidence.build_screening_context(candidate, "policy version one")
    assert evidence.build_screening_context(candidate, "policy version one") == first
    changed = evidence.build_screening_context(candidate, "policy version two")
    assert changed["screening_evidence_sha256"] != first["screening_evidence_sha256"]
    candidate["issue_comments"][0]["body"] += "\nThis requirement is withdrawn."
    assert evidence.build_screening_context(candidate, "policy version one")["screening_evidence_sha256"] != first["screening_evidence_sha256"]


def test_missing_safety_answer_is_retryable_even_with_eligible_yes(monkeypatch):
    model_response(monkeypatch, missing="private_disclosure")
    result = OpenJevClient().score_issue({"screening_context_version": 2})
    assert result["usable"] is None


def test_semantic_questions_reject_keyword_shortcuts_and_public_issue_authorization(monkeypatch):
    calls = model_response(monkeypatch)
    OpenJevClient().score_issue({"screening_context_version": 2})
    instructions = " ".join(question["instructions"] for question in calls[0]["questions"].values())
    assert "public issue" in instructions and "not authorization" in instructions
    assert "COLLABORATOR" in instructions and "Already merged" in instructions
    assert "Chinese/English" in instructions and "keywords alone" in instructions


def test_feasibility_screening_requires_evidence_not_environment_or_language_shortcuts(monkeypatch):
    calls = model_response(monkeypatch)
    OpenJevClient().score_issue({"screening_context_version": 2})
    questions = calls[0]["questions"]
    assert set(questions) == {"eligible", "private_disclosure", "unmet_prerequisite", "prerequisite_unknown"}
    instructions = questions["eligible"]["instructions"]
    assert "Reject feasibility only when concrete evidence establishes" in instructions
    assert "service cannot be obtained" in instructions and "broad architectural change" in instructions
    assert "not repository name" in instructions and "programming language" in instructions
    assert "normal dependency installation and code investigation belong to repair" in instructions
    assert "Do not demand a completed code investigation" in instructions
    assert "or an unavailable test environment" not in instructions


def test_original_qwen_path_evidence_reaches_feasibility_question_without_rewriting(monkeypatch):
    candidate = item("qwen_prerequisite")
    # The real final review found getHistoryShallow copying Part objects; idealized
    # direct metadata attachment did not exercise loss along that actual path.
    candidate["issue_comments"].append({
        "author": "reviewer", "association": "COLLABORATOR", "author_type": "User",
        "body": "getHistoryShallow copies Part objects with spread. Non-enumerable recovery handles "
                "are lost here. 直接给中间对象加元数据不会覆盖真实入口的复制路径。",
    })
    policy_reads(monkeypatch, candidate["repo"], merged=True)
    calls = model_response(monkeypatch)
    result = data.fine_screen_issue(SimpleNamespace(), candidate, lane="open")
    assert result["usable"] is True  # Test transport, not a mocked claim of model reasoning.
    assert calls[0]["state"]["comments"][-1]["body"] == candidate["issue_comments"][-1]["body"]
    instructions = calls[0]["questions"]["eligible"]["instructions"]
    assert "real user-facing entry point" in instructions
    assert "idealized intermediate object" in instructions
    assert "parsing, serialization, copying or adapters" in instructions
