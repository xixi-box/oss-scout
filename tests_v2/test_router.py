"""路由测试（计划 §5 矩阵 + §16：broad_scan 拒 Actions、回退仅限平台问题）。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scout_v2.job_router import JobRouter, RouterConfig  # noqa: E402


def cfg(**kw) -> RouterConfig:
    base = dict(modal_enabled=False, actions_enabled=False, modal_budget_ok=False,
                actions_eligible=False, modal_platform_ok=True)
    base.update(kw)
    return RouterConfig(**base)


def job(mode, scope="project_contribution") -> dict:
    return {"mode": mode, "execution_scope": scope, "base_sha": "abc"}


class TestBroadScan:
    @pytest.mark.parametrize("modal,actions", [(True, True), (False, True)])
    def test_broad_scan_never_actions(self, modal, actions):
        r = JobRouter(cfg(modal_enabled=modal, modal_budget_ok=modal,
                          actions_enabled=actions, actions_eligible=actions))
        d = r.route(job("scan", "broad_scan"))
        assert d.backend != "github_actions"

    def test_broad_scan_pauses_when_modal_blocked(self):
        d = JobRouter(cfg(modal_enabled=True, modal_budget_ok=False)).route(
            job("scan", "broad_scan"))
        assert d.backend == "pause" and "budget" in d.reason

    def test_broad_scan_modal_ok(self):
        d = JobRouter(cfg(modal_enabled=True, modal_budget_ok=True)).route(
            job("scan", "broad_scan"))
        assert d.backend == "modal"

    def test_broad_scan_only_mode_scan(self):
        d = JobRouter(cfg(modal_enabled=True, modal_budget_ok=True)).route(
            job("reproduce", "broad_scan"))
        assert d.backend == "pause"


class TestReproduce:
    def test_reproduce_actions_primary(self):
        """用户裁定（2026-10-31）：分工独占——reproduce 主选 Actions。"""
        r = JobRouter(cfg(actions_enabled=True, actions_eligible=True,
                          modal_enabled=True, modal_budget_ok=True))
        d = r.route(job("reproduce"))
        assert d.backend == "github_actions"

    def test_reproduce_pauses_instead_of_falling_back(self):
        """Actions 不可用 → pause，绝不静默换到 Modal（那里 opencode 跑不通）。"""
        r = JobRouter(cfg(actions_enabled=True, actions_eligible=False,
                          modal_enabled=True, modal_budget_ok=True))
        d = r.route(job("reproduce"))
        assert d.backend == "pause"
        assert "ineligible" in d.reason

    def test_repair_pauses_instead_of_falling_back(self):
        """对称：repair 只走 Actions。Modal 兜底曾在实测里零成功。"""
        r = JobRouter(cfg(actions_enabled=True, actions_eligible=False,
                          modal_enabled=True, modal_budget_ok=True))
        d = r.route(job("repair"))
        assert d.backend == "pause"

    def test_pauses_when_both_blocked(self):
        r = JobRouter(cfg(actions_enabled=True, actions_eligible=False,
                          modal_enabled=True, modal_budget_ok=False))
        d = r.route(job("ci"))
        assert d.backend == "pause"

    def test_p1_default_local(self):
        # P1：所有 feature flag 关闭 → local（骨架），绝不 Actions
        d = JobRouter(cfg()).route(job("review"))
        assert d.backend == "local"

    def test_invalid_mode_rejected(self):
        d = JobRouter(cfg()).route({"mode": "yolo"})
        assert d.backend == "pause" and "invalid mode" in d.reason

    def test_scan_modal_only_regardless_of_scope(self):
        """scan 一律 Modal，连 project_contribution 也不落 Actions（2026-10-31
        用户裁定：互不兜底；Modal 被预算挡住就 pause）。"""
        r = JobRouter(cfg(actions_enabled=True, actions_eligible=True,
                          modal_enabled=True, modal_budget_ok=False))
        d = r.route(job("scan", "project_contribution"))
        assert d.backend == "pause" and "budget" in d.reason

    def test_scan_never_actions_even_when_modal_disabled(self):
        r = JobRouter(cfg(actions_enabled=True, actions_eligible=True,
                          modal_enabled=False))
        d = r.route(job("scan", "broad_scan"))
        assert d.backend == "pause"


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
            "acme/real#1": {"id": "acme/real#1", "repo": "acme/real", "number": 1,
                            "status": "new", "blockers": [],
                            "ai_verdict": {"usable": True, "reason": "clear bug"}},
        }), encoding="utf-8")
        ready = drain_ai_graded(root, limit=5, dry_run=True)
        assert len(ready) == 1
        assert ready[0]["id"] == "acme/real#1"

    def test_ai_no_is_not_drained(self, tmp_path):
        """AI 判定 NO 的候选不进 drain。"""
        import json as _json
        from scout_v2.workers.auto_pipeline import drain_ai_graded

        root = tmp_path
        (root / "queue").mkdir(parents=True)
        (root / "queue" / "candidates.json").write_text(_json.dumps({
            "acme/real#2": {"id": "acme/real#2", "repo": "acme/real", "number": 2,
                            "status": "new", "blockers": [],
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
            "acme/real#3": {"id": "acme/real#3", "repo": "acme/real", "number": 3,
                            "status": "new", "blockers": ["docs_only"],
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

    def test_staged_come_before_fresh(self, tmp_path):
        """暂存项必须排在当天新扫的前面。"""
        from scout_v2.workers.auto_pipeline import drain_ai_graded
        root = self._root(tmp_path, {
            "a/fresh#2": {"id": "a/fresh#2", "repo": "a/fresh", "number": 2,
                          "status": "new", "blockers": [],
                          "discovered_at": self._ts(0.1),
                          "ai_verdict": {"usable": True}},
            "a/old#1": {"id": "a/old#1", "repo": "a/old", "number": 1,
                        "status": "staged", "blockers": [],
                        "staged_at": self._ts(1.0),
                        "discovered_at": self._ts(1.0),
                        "ai_verdict": {"usable": True}},
        })
        ready = drain_ai_graded(root, limit=1, dry_run=True)
        assert [r["id"] for r in ready] == ["a/old#1"]

    def test_overflow_is_staged_not_dropped(self, tmp_path):
        """额度只剩 1 条时，新扫到的其余候选标 staged 并记时间。"""
        from scout_v2.workers.auto_pipeline import drain_ai_graded
        entries = {
            f"a/r#{i}": {"id": f"a/r#{i}", "repo": "a/r", "number": i,
                         "status": "new", "blockers": [],
                         "discovered_at": self._ts(0.05 * i),
                         "ai_verdict": {"usable": True}}
            for i in range(1, 4)
        }
        root = self._root(tmp_path, entries)
        drain_ai_graded(root, limit=1, dry_run=False)
        q = self._read(root)
        staged = [k for k, v in q.items() if v.get("status") == "staged"]
        assert len(staged) == 2
        assert all(q[k].get("staged_at") for k in staged)

    def test_expired_stage_is_dropped(self, tmp_path):
        """超过 3 天的暂存不再参与派发。"""
        from scout_v2.workers.auto_pipeline import drain_ai_graded
        root = self._root(tmp_path, {
            "a/stale#1": {"id": "a/stale#1", "repo": "a/stale", "number": 1,
                          "status": "staged", "blockers": [],
                          "staged_at": self._ts(5.0),
                          "ai_verdict": {"usable": True}},
        })
        assert drain_ai_graded(root, limit=5, dry_run=True) == []

    def test_existing_stage_keeps_original_timestamp(self, tmp_path):
        """已经在暂存里的不能被刷新 staged_at，否则永远不会过期。"""
        from scout_v2.workers.auto_pipeline import drain_ai_graded
        root = self._root(tmp_path, {
            "a/keep#1": {"id": "a/keep#1", "repo": "a/keep", "number": 1,
                         "status": "staged", "blockers": [],
                         "staged_at": self._ts(1.5),
                         "ai_verdict": {"usable": True}},
            "a/new#2": {"id": "a/new#2", "repo": "a/new", "number": 2,
                        "status": "new", "blockers": [],
                        "discovered_at": self._ts(0.02),
                        "ai_verdict": {"usable": True}},
        })
        drain_ai_graded(root, limit=1, dry_run=False)
        q = self._read(root)
        assert q["a/keep#1"]["staged_at"] == self._ts(1.5)


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
                "status": "new", "blockers": [],
                "ai_verdict": {"usable": True}},
        }), encoding="utf-8")
        assert drain_ai_graded(tmp_path, limit=5, dry_run=True) == []
