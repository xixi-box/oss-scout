"""路由测试（计划 §5 矩阵 + §16：broad_scan 拒 Actions、回退仅限平台问题）。"""
from __future__ import annotations
from scout_v2.state_store import business_now as queue_now

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scout_v2.job_router import JobRouter, RouterConfig  # noqa: E402


def cfg(**kw) -> RouterConfig:
    base = dict(actions_enabled=False, actions_eligible=False)
    base.update(kw)
    return RouterConfig(**base)


def job(mode, scope="project_contribution") -> dict:
    return {"mode": mode, "execution_scope": scope, "base_sha": "abc"}


class TestRouting:
    @pytest.mark.parametrize("mode,scope", [
        ("scan", "broad_scan"), ("scan", "project_contribution"),
        ("repair", "broad_scan")])
    def test_retired_scans_are_blocked(self, mode, scope):
        router = JobRouter(cfg(actions_enabled=True, actions_eligible=True))
        assert router.route(job(mode, scope)).backend == "pause"

    @pytest.mark.parametrize("mode", ["repair", "reproduce", "review", "ci"])
    def test_actions_only(self, mode):
        assert JobRouter(cfg(actions_enabled=True, actions_eligible=True)).route(job(mode)).backend == "github_actions"
        assert JobRouter(cfg(actions_enabled=True, actions_eligible=False)).route(job(mode)).backend == "pause"

    def test_invalid_mode_rejected(self):
        result = JobRouter(cfg()).route(job("yolo"))
        assert result.backend == "pause" and "invalid mode" in result.reason

    def test_legacy_local_fallback_when_disabled(self):
        assert JobRouter(cfg()).route(job("review")).backend == "local"


class TestRepairLoopConcurrency:
    """修复循环改成 60 秒轮询 + 后台线程派发后，必须保证不重复派同一条。

    2026-10-30：间隔从 300 秒压到 60 秒（对齐 Worker 的每分钟扫描）。pipeline
    是长任务，如果让它阻塞轮询，一条慢修复会把发现节奏拖成几小时；所以改成
    轮询只负责派发、每条候选一个线程。代价是必须自己防重复派发。
    """

    def test_drain_dry_run_returns_id_for_dedup(self, tmp_path):
        """dry_run 的返回必须带 id，否则无法在 inflight 表里去重。"""
        import json as _json
        from pathlib import Path as _P
        from scout_v2.workers.auto_pipeline import drain_ai_graded

        root = tmp_path
        (root / "queue").mkdir(parents=True)
        (root / "queue" / "candidates.json").write_text(_json.dumps({
            "traefik/traefik#1": {"id": "traefik/traefik#1", "repo": "traefik/traefik", "number": 1,
                            "status": "staged", "eligible_at": "2000-01-01T00:00:00+08:00", "screened_at": queue_now().isoformat(), "blockers": [],
                            "discovered_at": queue_now().isoformat(),
                            "ai_verdict": {"usable": True, "reason": "clear bug"}},
        }), encoding="utf-8")
        ready = drain_ai_graded(root, limit=5, dry_run=True)
        assert len(ready) == 1
        assert ready[0]["id"] == "traefik/traefik#1"

    def test_ai_no_is_not_drained(self, tmp_path):
        """AI 判定 NO 的候选不进 drain。"""
        import json as _json
        from scout_v2.workers.auto_pipeline import drain_ai_graded

        root = tmp_path
        (root / "queue").mkdir(parents=True)
        (root / "queue" / "candidates.json").write_text(_json.dumps({
            "traefik/traefik#2": {"id": "traefik/traefik#2", "repo": "traefik/traefik", "number": 2,
                            "status": "staged", "eligible_at": "2000-01-01T00:00:00+08:00", "screened_at": queue_now().isoformat(), "blockers": [],
                            "ai_verdict": {"usable": False, "reason": "needs design"}},
        }), encoding="utf-8")
        assert drain_ai_graded(root, limit=5, dry_run=True) == []

    def test_blockers_still_veto(self, tmp_path):
        """客观否决优先于 AI 结论：有 blockers 就不进 drain。"""
        import json as _json
        from scout_v2.workers.auto_pipeline import drain_ai_graded

        root = tmp_path
        (root / "queue").mkdir(parents=True)
        (root / "queue" / "candidates.json").write_text(_json.dumps({
            "traefik/traefik#3": {"id": "traefik/traefik#3", "repo": "traefik/traefik", "number": 3,
                            "status": "staged", "eligible_at": "2000-01-01T00:00:00+08:00", "screened_at": queue_now().isoformat(), "blockers": ["docs_only"],
                            "ai_verdict": {"usable": True, "reason": "looks fine"}},
        }), encoding="utf-8")
        assert drain_ai_graded(root, limit=5, dry_run=True) == []


class TestOutOfScopeIsTerminal:
    """lane=unknown/observe 必须写终态，否则每轮重试。

    2026-10-30 实测：仓库名单从 173 换成 40 个后，旧名单遗留的
    anomalyco/opencode、deepset-ai/haystack 候选，在 60 秒循环下每分钟
    被取一次、每次都 lane=unknown 空转。原因是那条分支直接 return，
    没改状态，drain 下一轮又选到它。
    """

    def test_terminal_statuses_excluded_from_drain(self):
        import json as _json
        from pathlib import Path as _P
        from scout_v2.workers.auto_pipeline import drain_ai_graded

        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = _P(td)
            (root / "queue").mkdir(parents=True)
            (root / "queue" / "candidates.json").write_text(_json.dumps({
                "gone/repo#1": {"id": "gone/repo#1", "repo": "gone/repo",
                                "number": 1, "status": "out_of_scope",
                                "blockers": [], "ai_verdict": {"usable": True}},
                "obs/repo#2": {"id": "obs/repo#2", "repo": "obs/repo",
                               "number": 2, "status": "observe_only",
                               "blockers": [], "ai_verdict": {"usable": True}},
            }), encoding="utf-8")
            assert drain_ai_graded(root, limit=5, dry_run=True) == []


class TestStagedCandidates:
    """额度用完后的候选暂存：今天做不完的留到明天，不是丢弃也不是反复扫描。

    2026-10-30（用户裁定）：每天至多 2 个 PR。当天额度用完之后又识别出可处理的
    issue，先存着，第二天**先清暂存**；暂存空了才接当天新扫的。暂存超过 3 天
    丢弃——放三天的 issue 多半已经被人修了或关了。

    时间戳一律用相对时间生成：硬编码日期会被真实时间追上（2026-11-01 实测，
    "2026-10-29" 在两天后落进 3 天 TTL 被当过期丢弃，测试自己红掉）。
    """

    @staticmethod
    def _ts(days_ago: float) -> str:
        import time as _t
        return _t.strftime("%Y-%m-%dT%H:%M:%S", _t.localtime(_t.time() - days_ago * 86400))

    def _root(self, tmp_path, entries):
        import json as _json
        (tmp_path / "queue").mkdir(parents=True)
        (tmp_path / "queue" / "candidates.json").write_text(
            _json.dumps(entries), encoding="utf-8")
        return tmp_path

    def _read(self, root):
        import json as _json
        return _json.loads((root / "queue" / "candidates.json").read_text(encoding="utf-8"))

    def test_fresh_comes_before_historical_backlog(self, tmp_path):
        """Ordinary dispatch only consumes today's discoveries."""
        from scout_v2.workers.auto_pipeline import drain_ai_graded
        root = self._root(tmp_path, {
            "traefik/traefik#2": {"id": "traefik/traefik#2", "repo": "traefik/traefik", "number": 2,
                          "status": "staged", "eligible_at": "2000-01-01T00:00:00+08:00", "screened_at": queue_now().isoformat(), "blockers": [],
                          "discovered_at": queue_now().isoformat(),
                          "ai_verdict": {"usable": True}},
            "traefik/traefik#1": {"id": "traefik/traefik#1", "repo": "traefik/traefik", "number": 1,
                        "status": "staged", "blockers": [],
                        "staged_at": self._ts(1.0),
                        "eligible_at": self._ts(0.2),
                        "discovered_at": self._ts(1.0),
                        "ai_verdict": {"usable": True}},
        })
        ready = drain_ai_graded(root, limit=1, dry_run=True)
        assert [r["id"] for r in ready] == ["traefik/traefik#2"]

    def test_batch_limit_does_not_spend_daily_quota(self, tmp_path):
        """额度只剩 1 条时，新扫到的其余候选标 staged 并记时间。"""
        from scout_v2.workers.auto_pipeline import drain_ai_graded
        entries = {
            f"traefik/traefik#{i}": {"id": f"traefik/traefik#{i}", "repo": "traefik/traefik", "number": i,
                         "status": "staged", "eligible_at": "2000-01-01T00:00:00+08:00", "screened_at": queue_now().isoformat(), "blockers": [],
                         "discovered_at": self._ts(0.05 * i),
                         "ai_verdict": {"usable": True}}
            for i in range(1, 4)
        }
        root = self._root(tmp_path, entries)
        drain_ai_graded(root, limit=1, dry_run=False)
        q = self._read(root)
        staged = [k for k, v in q.items() if v.get("status") == "staged"]
        assert len(staged) == 3
        assert all(q[k].get("defer_reason") != "daily_quota" for k in staged)

    def test_expired_stage_is_dropped(self, tmp_path):
        """超过 3 天的暂存不再参与派发。"""
        from scout_v2.workers.auto_pipeline import drain_ai_graded
        root = self._root(tmp_path, {
            "traefik/traefik#1": {"id": "traefik/traefik#1", "repo": "traefik/traefik", "number": 1,
                          "status": "staged", "blockers": [],
                          "staged_at": self._ts(5.0),
                          "ai_verdict": {"usable": True}},
        })
        assert drain_ai_graded(root, limit=5, dry_run=True) == []

    def test_existing_stage_keeps_original_timestamp(self, tmp_path):
        """已经在暂存里的不能被刷新 staged_at，否则永远不会过期。"""
        from scout_v2.workers.auto_pipeline import drain_ai_graded
        root = self._root(tmp_path, {
            "traefik/traefik#1": {"id": "traefik/traefik#1", "repo": "traefik/traefik", "number": 1,
                         "status": "staged", "blockers": [],
                         "staged_at": self._ts(1.5),
                         "ai_verdict": {"usable": True}},
            "traefik/traefik#2": {"id": "traefik/traefik#2", "repo": "traefik/traefik", "number": 2,
                        "status": "staged", "eligible_at": "2000-01-01T00:00:00+08:00", "screened_at": queue_now().isoformat(), "blockers": [],
                        "discovered_at": self._ts(0.02),
                        "ai_verdict": {"usable": True}},
        })
        original_staged_at = self._read(root)["traefik/traefik#1"]["staged_at"]
        drain_ai_graded(root, limit=1, dry_run=False)
        q = self._read(root)
        assert q["traefik/traefik#1"]["staged_at"] == original_staged_at


class TestInternalRepoExcluded:
    """fixture 仓库不进自动 drain：演练轮也在烧 Actions 分钟。"""

    def test_fixture_repo_not_drained(self, tmp_path):
        import json as _json
        from scout_v2.workers.auto_pipeline import drain_ai_graded
        (tmp_path / "queue").mkdir(parents=True)
        (tmp_path / "queue" / "candidates.json").write_text(_json.dumps({
            "xixi-box/oss-scout-v2-test#9": {
                "id": "xixi-box/oss-scout-v2-test#9",
                "repo": "xixi-box/oss-scout-v2-test", "number": 9,
                "status": "staged", "eligible_at": "2000-01-01T00:00:00+08:00", "screened_at": queue_now().isoformat(), "blockers": [],
                "ai_verdict": {"usable": True}},
        }), encoding="utf-8")
        assert drain_ai_graded(tmp_path, limit=5, dry_run=True) == []
