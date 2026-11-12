import pytest

from scout_v2.console_models import write_models, pr_comment_trigger_enabled
from test_pr_monitor import mp, _pr, _comment, _check


def test_disabled_switch_blocks_ai_classification_drafts_and_all_comment_dispatches(tmp_path, monkeypatch):
    write_models(tmp_path, {"pr_comment_trigger": "off"})
    monkeypatch.setattr(mp, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(mp, "classify_comment", lambda *a, **kw: pytest.fail("AI must stay off"))
    monkeypatch.setattr(mp, "_thread", lambda *a, **kw: pytest.fail("must not prepare replies"))
    assert not pr_comment_trigger_enabled(tmp_path)
    result = mp.classify(_pr(comments=[_comment("maintainer", "please fix this")]))
    assert result["action"] == "wait" and "disabled" in result["reason"]
    for kind in ("followup", "review"):
        assert not mp.dispatch_pr_fix("docker/compose", 7, kind=kind, force=True)["dispatched"]
    assert not mp.draft_reply("docker/compose", 7, force=True)["drafted"]
    # PR/CI monitoring continues independently of comment-driven automation.
    assert mp.classify(_pr(rollup=[_check("tests", "FAILURE")]))["action"] == "fix_ci"
    monkeypatch.setattr(mp, "monitored_repo", lambda _: True)
    monkeypatch.setattr(mp, "ATTEMPTS_FILE", tmp_path / "state/attempts.json")
    monkeypatch.setattr(mp, "_dispatch_pr_fix", lambda *a, **kw: {"dispatched": True, "kind": kw["kind"]})
    assert mp.dispatch_pr_fix("docker/compose", 7, kind="ci")["dispatched"]


def test_switch_defaults_on_and_reloads_without_restart(tmp_path):
    assert pr_comment_trigger_enabled(tmp_path)
    write_models(tmp_path, {"pr_comment_trigger": "off"})
    assert not pr_comment_trigger_enabled(tmp_path)
    write_models(tmp_path, {"pr_comment_trigger": "on"})
    assert pr_comment_trigger_enabled(tmp_path)
