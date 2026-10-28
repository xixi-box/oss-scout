"""策略引擎测试：AGENTS.md fail-closed、闸门池、新鲜度、每日认领、禁止文案、403 blocker。"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))

from legacy_samples import LEGACY_WORKLOG_LINES  # noqa: E402
from scout_v2.repo_policy import PolicyEngine  # noqa: E402
from scout_v2.state_store import ClaimLedger, Worklog  # noqa: E402

REPOS_CFG = {
    "defaults": {"unknown_repo_action": "hold_for_review", "min_issue_age_hours": 2,
                 "max_new_claims_per_business_day": 1},
    "repositories": {
        "pydantic/pydantic-ai": {"lane": "gated"},
        "BerriAI/litellm": {"lane": "restricted", "only": "maintainer_confirmed"},
        "aaif-goose/goose": {"lane": "open"},
        "google-gemini/gemini-cli": {"lane": "observe"},
    },
}
V2_CFG = {"business_timezone": "Asia/Shanghai"}


def make_engine(tmp_path, agents_md="# playbook\nrules here", worklog_lines=None) -> PolicyEngine:
    wl = Worklog(tmp_path / "worklog.jsonl")
    for line in worklog_lines or []:
        wl.path.write_text("\n".join(worklog_lines or []) + "\n", encoding="utf-8")
    db = ClaimLedger(tmp_path / "ledger.sqlite3")
    eng = PolicyEngine(tmp_path, {}, REPOS_CFG, V2_CFG, agents_md)
    eng._db = db
    eng._worklog = wl
    return eng


class TestFailClosed:
    def test_missing_agents_md_blocks_all_writes(self):
        eng = make_engine(Path("/tmp"), agents_md="")
        d = eng.authorize("open_pr", "aaif-goose/goose")
        assert not d.allowed and "AGENTS.md" in d.reason

    def test_unparseable_agents_md_blocks_claim(self):
        eng = make_engine(Path("/tmp"), agents_md="   ")
        d = eng.authorize("claim", "aaif-goose/goose")
        assert not d.allowed

    def test_unknown_repo_hold(self):
        eng = make_engine(Path("/tmp"))
        d = eng.authorize("open_pr", "some/unknown-repo")
        assert not d.allowed and "hold_for_review" in d.reason

    def test_observe_only_no_writes(self):
        eng = make_engine(Path("/tmp"))
        d = eng.authorize("comment_analysis", "google-gemini/gemini-cli")
        assert not d.allowed


class TestPermanentDenials:
    @pytest.mark.parametrize("action", ["push_force", "merge_upstream", "skip_ci"])
    def test_always_denied(self, action):
        eng = make_engine(Path("/tmp"))
        assert not eng.authorize(action, "aaif-goose/goose").allowed


class TestGatedPool:
    def test_gated_no_direct_pr(self):
        eng = make_engine(Path("/tmp"))
        d = eng.authorize("open_pr", "pydantic/pydantic-ai")
        assert not d.allowed and "await assignment" in d.reason

    def test_no_please_assign_me_text(self):
        d = PolicyEngine.check_comment_text("Could you please assign me this issue?")
        assert not d.allowed
        d2 = PolicyEngine.check_comment_text(
            "I reproduced this on v1.0. Happy to prepare a PR if that approach works.")
        assert d2.allowed


class TestRestricted:
    def test_restricted_requires_confirm(self):
        eng = make_engine(Path("/tmp"))
        assert not eng.authorize("open_pr", "BerriAI/litellm").allowed
        assert eng.authorize("open_pr", "BerriAI/litellm",
                             maintainer_confirmed=True).allowed


class TestFreshness:
    def test_issue_younger_than_2h_rejected(self):
        eng = make_engine(Path("/tmp"))
        now = dt.datetime.now(dt.timezone.utc)
        fresh = (now - dt.timedelta(minutes=90)).isoformat()
        d = eng.check_freshness(fresh, now=now)
        assert not d.allowed and "age" in d.reason

    def test_issue_older_than_2h_ok(self):
        eng = make_engine(Path("/tmp"))
        now = dt.datetime.now(dt.timezone.utc)
        old = (now - dt.timedelta(hours=5)).isoformat()
        assert eng.check_freshness(old, now=now).allowed


class TestDailyClaimLimit:
    def test_worklog_claim_today_blocks_new_claim(self, tmp_path):
        today = dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        lines = [f'{{"ts": "{today}", "action": "claim", "target": "aaif-goose/goose#1"}}']
        eng = make_engine(tmp_path, worklog_lines=lines)
        assert not eng.authorize("claim", "aaif-goose/goose").allowed

    def test_awaiting_assignment_counts_as_claim(self, tmp_path):
        today = dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        lines = [f'{{"ts": "{today}", "action": "awaiting_assignment", "target": "x#2"}}']
        eng = make_engine(tmp_path, worklog_lines=lines)
        assert not eng.authorize("claim", "aaif-goose/goose").allowed

    def test_yesterday_claim_does_not_block(self, tmp_path):
        yesterday = (dt.datetime.now() - dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
        lines = [f'{{"ts": "{yesterday}", "action": "claim", "target": "aaif-goose/goose#1"}}']
        eng = make_engine(tmp_path, worklog_lines=lines)
        assert eng.authorize("claim", "aaif-goose/goose").allowed

    def test_legacy_nonclaim_lines_do_not_block(self, tmp_path):
        eng = make_engine(tmp_path, worklog_lines=LEGACY_WORKLOG_LINES)
        assert eng.authorize("claim", "aaif-goose/goose").allowed


class TestBlockerRegistry:
    def test_403_blocker_stops_retry_this_run(self, tmp_path):
        from scout_v2.state_store import BlockerRegistry
        eng = make_engine(tmp_path)
        reg = BlockerRegistry(tmp_path / "blockers.sqlite3")
        reg.register("aaif-goose/goose:push", "403 Resource not accessible by integration")
        d = eng.authorize("push", "aaif-goose/goose", blocker_registry=reg)
        assert not d.allowed and "blocker" in d.reason


class TestPolicySnapshot:
    def test_snapshot_changes_when_playbook_changes(self, tmp_path):
        eng1 = make_engine(tmp_path, agents_md="rule v1")
        eng2 = make_engine(tmp_path, agents_md="rule v2")
        assert eng1.snapshot_id() != eng2.snapshot_id()


class TestInternalRepoDoesNotConsumeQuota:
    """内部测试仓库的演练不应占用真实仓库每天 1 个的认领额度。

    2026-10-28 实录：在 fixture 上跑了 6 次闭环演练，当天真实 issue 被日闸拦下
    （"day 2026-10-28 already claimed by xixi-box/oss-scout-v2-test#8"）。
    演练是为了验证机制，不该把它当成一次对外承诺。
    """

    def _engine(self, tmp_path, internal: bool):
        import json as _json
        root = tmp_path
        (root / "state").mkdir(parents=True, exist_ok=True)
        repos = {"repositories": {
            "acme/real": {"lane": "open"},
            "me/fixture": {"lane": "open", "internal_test_repo": internal},
        }}
        (root / "configs").mkdir(exist_ok=True)
        (root / "configs" / "repositories.yaml").write_text(
            "repositories:\n"
            '  acme/real: {lane: open}\n'
            f'  me/fixture: {{lane: open, internal_test_repo: {str(internal).lower()}}}\n',
            encoding="utf-8")
        (root / "config.json").write_text(_json.dumps({"open_pool": [
            {"repo": "acme/real"}, {"repo": "me/fixture"}]}), encoding="utf-8")
        (root / "configs" / "v2.yaml").write_text(
            "budget: {}\nfeatures: {}\ntimezone: Asia/Shanghai\n", encoding="utf-8")
        (root / "AGENTS.md").write_text("# playbook", encoding="utf-8")
        return repos

    def test_fixture_claim_is_ignored(self, tmp_path, monkeypatch):
        from scout_v2.repo_policy import PolicyEngine
        from scout_v2.state_store import Worklog
        import datetime as dt

        self._engine(tmp_path, internal=True)
        wl = Worklog(tmp_path / "state" / "worklog.jsonl")
        wl.append({"ts": dt.datetime.now().isoformat(timespec="seconds"),
                   "action": "claim", "target": "me/fixture#1"})
        eng, err = PolicyEngine.load(tmp_path)
        assert err is None, err
        eng._worklog = wl
        assert eng.claim_slot_available() is True, "演练不该吃掉真实额度"

    def test_real_claim_still_blocks(self, tmp_path):
        from scout_v2.repo_policy import PolicyEngine
        from scout_v2.state_store import Worklog
        import datetime as dt

        self._engine(tmp_path, internal=True)
        wl = Worklog(tmp_path / "state" / "worklog.jsonl")
        wl.append({"ts": dt.datetime.now().isoformat(timespec="seconds"),
                   "action": "claim", "target": "acme/real#7"})
        eng, err = PolicyEngine.load(tmp_path)
        assert err is None, err
        eng._worklog = wl
        assert eng.claim_slot_available() is False, "真实认领必须守住每日 1 个"
