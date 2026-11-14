"""Regression: agentscope-java #4162/#4163 author intent, without upstream writes."""
import io
import json
from types import SimpleNamespace

import pytest

from dashboard import data
from scout_v2 import git_host
from scout_v2.contribution_intent import claim_evidence, self_fix_evidence, coordination_evidence, MULTILINGUAL_REVIEW_RULES
from scout_v2.workers.openjev import OpenJevClient


ZH_OFFER = "如果没有正在进行的同类工作，我愿意提交一个聚焦该问题的修复和回归测试。"
EN_OFFER = "If nobody is working on this, I would like to submit a focused fix and regression test."
ZH_COMMENT = (
    "感谢你关注 #4162，也谢谢你提交修复。我在报告这个问题时提到过愿意提供修复和回归测试，"
    "现在已经把自己的实现提交到了 #4167。\n\n"
    "这次我想继续跟进 #4167，就不另在 #4163 上推进了。为避免两边重复投入，"
    "如果你有建议，欢迎直接在 #4167 里讨论。最终采用哪份实现，以维护者的评审意见为准。谢谢理解！"
)
EN_COMMENT = "Thanks! I submitted my own implementation in #4167. Let's avoid duplicate work and discuss it there."


def recheck(monkeypatch, body, comments=()):
    issue = {"number": 4162, "title": "Tool call IDs differ", "state": "open", "assignees": [],
             "labels": [], "user": {"login": "reporter"}, "author_association": "CONTRIBUTOR", "body": body}
    monkeypatch.setattr(git_host, "gh_read", lambda args: {"items": []} if args[0].startswith("search/") else issue)
    monkeypatch.setattr(git_host, "gh_pages", lambda path, *a: list(comments) if path.endswith("/comments") else [])
    return git_host.recheck_candidate("agentscope-ai/agentscope-java", 4162)


@pytest.mark.parametrize("offer", [ZH_OFFER, EN_OFFER, "这个问题我来修复。", "我计划提交修复 PR。", "我可以修复。"])
def test_self_offer_blocks_before_ai(monkeypatch, offer):
    # Put the genuine author-intent paragraph after a long fenced reproduction.
    body = "Reproduction:\n```java\n" + "assertEquals(id, actual);\n" * 450 + "```\n" + offer
    result = recheck(monkeypatch, body)
    assert result.complete and result.has_claim_signal
    assert data.live_issue_blockers(result) == ["claim_signal"]
    monkeypatch.setattr(OpenJevClient, "score_issue", lambda *a: pytest.fail("coarse rejection must not call AI"))
    eng = SimpleNamespace(repos_cfg={"repositories": {"agentscope-ai/agentscope-java": {}}})
    blockers, verdict = data.screen_issue(eng, {"repo": "agentscope-ai/agentscope-java", "number": 4162},
                                         lane="open", live_blockers=data.live_issue_blockers(result))
    assert blockers == ["claim_signal"] and verdict["reason"] == "not screened"


@pytest.mark.parametrize("body", [
    "我可以复现这个问题，并提供日志。", "I can reproduce this and provide logs.",
    "我可以提供日志供你修复这个问题。", "I can provide logs to help you fix this.",
    "我无法修复这个问题，希望有人帮忙。", "I will not fix this issue.",
    "I'll reproduce this and provide a screenshot.",
    "> I would like to submit a fix.\nThis is a quotation, not my offer.",
    "```text\n我愿意提交修复和回归测试。\n```\nThis example is test data.",
    "The output is `I can fix this`.",
])
def test_noncommitments_and_quoted_examples_do_not_block(monkeypatch, body):
    assert not claim_evidence(body, pattern=git_host.RE_CLAIM)
    assert not self_fix_evidence(body)
    assert not recheck(monkeypatch, body).has_claim_signal


def test_other_contributor_chinese_claim_is_respected(monkeypatch):
    result = recheck(monkeypatch, "A reproducible bug", [
        {"user": {"login": "another-developer"}, "body": ZH_OFFER, "author_association": "NONE"}])
    assert result.has_claim_signal


@pytest.mark.parametrize("offer", [ZH_OFFER, EN_OFFER])
def test_fine_screen_receives_original_language_and_evidence(monkeypatch, offer):
    captured = []
    monkeypatch.setattr(OpenJevClient, "score_issue", lambda _, state: captured.append(state) or {"usable": False})
    monkeypatch.setattr(data, "repo_rules_text", lambda _: "rules")
    body = "Reproduction:\n```java\n" + "assertEquals(id, actual);\n" * 450 + "```\n" + offer
    item = {"repo": "agentscope-ai/agentscope-java", "number": 4162, "title": "ID mismatch",
            "body_excerpt": body, "reporter": "reporter", "issue_comments": []}
    data.fine_screen_issue(SimpleNamespace(), item, lane="open")
    assert captured[0]["body"] == body
    assert captured[0]["reporter_offered_fix"] is True
    assert captured[0]["reporter_offer_evidence"]
    assert captured[0]["claimish_signal"] is True


def test_fine_screen_no_match_is_unknown_and_reporter_comments_are_included(monkeypatch):
    captured = []
    monkeypatch.setattr(OpenJevClient, "score_issue", lambda _, state: captured.append(state) or {"usable": False})
    monkeypatch.setattr(data, "repo_rules_text", lambda _: "rules")
    item = {"repo": "agentscope-ai/agentscope-java", "number": 4162, "body_excerpt": "这个问题有明确复现。",
            "reporter": "reporter", "issue_comments": []}
    data.fine_screen_issue(SimpleNamespace(), item, lane="open")
    assert captured[-1]["reporter_offered_fix"] is None
    assert captured[-1]["claimish_signal"] is None
    item["issue_comments"] = [{"author": "reporter", "body": ZH_OFFER}]
    data.fine_screen_issue(SimpleNamespace(), item, lane="open")
    assert captured[-1]["reporter_offered_fix"] is True
    assert captured[-1]["comments"][0]["body"] == ZH_OFFER
    assert captured[-1]["comments"][0]["is_reporter"] is True












def test_semantic_issue_prompt_does_not_depend_on_rule_matches(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    def response(request, **kwargs):
        seen.append(json.loads(request.data))
        return io.BytesIO(b'{"answers":{"eligible":{"noul":0.01}}}')
    monkeypatch.setattr("scout_v2.workers.openjev.urllib.request.urlopen", response)
    state = {"body": ZH_OFFER, "reporter_offered_fix": None}
    assert OpenJevClient(tmp_path).score_issue(state)["usable"] is False
    assert seen[0]["state"] == state
    instructions = seen[0]["questions"]["eligible"]["instructions"]
    assert "UNKNOWN" in instructions and "Chinese, English" in instructions and "conditional offers" in instructions
    assert "coordination" in MULTILINGUAL_REVIEW_RULES and "requested_change must be false" in MULTILINGUAL_REVIEW_RULES




def test_legacy_semantic_scorer_does_not_turn_missing_hint_into_no(monkeypatch):
    from scout_v2.workers.issue_scorer import GoClient
    seen = []
    monkeypatch.setattr(GoClient, "chat", lambda _, prompt: seen.append(prompt) or (True, "DECISION: NO\nREASON: author is preparing a fix"))
    result = GoClient(key="test").score_issue({"body_excerpt": ZH_OFFER})
    assert result["usable"] is False
    assert "unknown; infer from original text" in seen[0]
    assert ZH_OFFER in seen[0]
