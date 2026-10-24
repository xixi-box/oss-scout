"""历史兼容测试：真实 state 文件可读、旧候选可规范化、调度守门不破坏 Playbook。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
ROOT = Path(__file__).resolve().parents[1]

from scout_v2.candidate_normalizer import (candidate_to_legacy,  # noqa: E402
                                           legacy_to_candidate)
from scout_v2.state_store import Worklog  # noqa: E402


class TestRealStateFiles:
    def test_real_worklog_fully_readable(self):
        """历史 worklog 必须完整可读（坏行保留在文件中、读取不抛）。"""
        wl = Worklog(ROOT / "state" / "worklog.jsonl")
        recs = wl.read_all()
        assert len(recs) >= 250  # P0 审计实测 258 行

    def test_real_legacy_queue_readable(self):
        from scout_v2.candidate_normalizer import load_legacy_queue
        cands = load_legacy_queue(ROOT / "queue" / "candidates.json")
        assert len(cands) > 0
        for c in cands.values():
            assert c.candidate_id.startswith("github:")
            assert c.repo and ":" in c.candidate_id

    def test_normalizer_roundtrip(self):
        from legacy_samples import LEGACY_QUEUE_ENTRY
        c = legacy_to_candidate(LEGACY_QUEUE_ENTRY)
        assert c.candidate_id == "github:pydantic/pydantic-ai:issue:7713"
        assert c.lane == "issue"
        back = candidate_to_legacy(c)
        assert back["id"] == LEGACY_QUEUE_ENTRY["id"]
        assert back["repo"] == LEGACY_QUEUE_ENTRY["repo"]
        assert back["class"] == "A"

    def test_bug_lane_has_no_legacy_mapping(self):
        from scout_v2.schemas import Candidate
        bug = Candidate(candidate_id="github:a/b:bug:sha1:fp", lane="bug", repo="a/b",
                        issue_number=None)
        with pytest.raises(ValueError):
            candidate_to_legacy(bug)


class TestSchedulerGuard:
    def test_playbook_pace_passes(self):
        from scout_v2.scheduler import scheduled_plan
        import yaml
        cfg = yaml.safe_load((ROOT / "configs" / "v2.yaml").read_text(encoding="utf-8"))
        plan = scheduled_plan(cfg)
        names = [t.name for t in plan]
        assert "observe_discover" in names and "consume_window" in names

    def test_aggressive_pace_rejected_until_playbook_revised(self):
        from scout_v2.scheduler import ScheduleConflictError, scheduled_plan
        with pytest.raises(ScheduleConflictError):
            scheduled_plan({"scheduler": {"observe_interval_minutes": 15}})

    def test_continuous_consume_rejected(self):
        from scout_v2.scheduler import ScheduleConflictError, scheduled_plan
        with pytest.raises(ScheduleConflictError):
            scheduled_plan({"scheduler": {}, "continuous_consume": True})
