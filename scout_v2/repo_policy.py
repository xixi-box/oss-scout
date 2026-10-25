"""仓库策略引擎（计划 §6）：只授予操作，不执行模型意图。

fail-closed 原则：
- AGENTS.md 缺失或解析失败 → 所有 GitHub 写动作拒绝。
- 未知仓库 → hold_for_review。
- 任何授权查询都即时判定，不缓存"允许"结论（缓存只用于拒绝）。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .state_store import ClaimLedger, Worklog

# 永久禁止的动作（Playbook §0.3：永不 force-push / 合并上游 / 跳过 CI）
ALWAYS_DENIED = {"push_force", "merge_upstream", "skip_ci"}
# GitHub 写动作白名单：authorize 只对白名单内动作给 allow
WRITE_ACTIONS = {"comment_analysis", "open_pr", "push", "fork", "claim",
                 "workflow_dispatch", "request_review"}
# 闸门池绝不允许的文案
FORBIDDEN_PHRASES = re.compile(r"please\s+assign\s+me|assign\s+this\s+(?:issue\s+)?to\s+me", re.I)


@dataclass
class Decision:
    allowed: bool
    reason: str
    action: str = ""
    repo: str = ""


class PolicyEngine:
    def __init__(self, root: Path, config_json: dict, repositories_yaml: dict,
                 v2_yaml: dict, agents_md: str | None = None):
        self.root = root
        self.config_json = config_json
        self.repos_cfg = repositories_yaml or {}
        self.v2_cfg = v2_yaml or {}
        self.agents_md_ok = bool(agents_md and agents_md.strip())
        self.agents_md_text = agents_md or ""
        self.tz_name = self.v2_cfg.get("business_timezone", "Asia/Shanghai")
        defaults = self.repos_cfg.get("defaults") or {}
        self.max_claims_per_day = int(defaults.get("max_new_claims_per_business_day", 1))
        self.min_issue_age_hours = float(defaults.get("min_issue_age_hours", 2))
        self.unknown_action = defaults.get("unknown_repo_action", "hold_for_review")
        self.agents_md_sha = (hashlib.sha256(self.agents_md_text.encode("utf-8")).hexdigest()[:16]
                              if self.agents_md_ok else "")

    # ---- 构造 ----

    @classmethod
    def load(cls, root: Path, db: ClaimLedger | None = None,
             worklog: Worklog | None = None) -> tuple["PolicyEngine", str | None]:
        """从项目目录加载全部配置。AGENTS.md 读不出 → 返回 fail-closed 引擎 + 错误说明。"""
        agents_err = None
        agents_md = None
        p = root / "AGENTS.md"
        try:
            agents_md = p.read_text(encoding="utf-8")
            if not agents_md.strip():
                agents_err = "AGENTS.md is empty"
        except OSError as e:
            agents_err = f"AGENTS.md unreadable: {e}"
        repos_cfg = _load_yaml(root / "configs" / "repositories.yaml") or {}
        # Dashboard 写入层（configs/lane_overrides.yaml）：用户经 Web 界面修改的 lane
        # 覆盖，叠加在 repositories.yaml 之上；每次修改都过 worklog 审计。
        overrides = _load_yaml(root / "configs" / "lane_overrides.yaml") or {}
        for repo, entry in (overrides.get("repositories") or {}).items():
            merged = dict((repos_cfg.get("repositories") or {}).get(repo) or {})
            merged.update(entry or {})
            repos_cfg.setdefault("repositories", {})[repo] = merged
        v2_cfg = _load_yaml(root / "configs" / "v2.yaml") or {}
        try:
            config_json = json_load(root / "config.json")
        except Exception as e:  # noqa: BLE001
            config_json = {}
            agents_err = agents_err or f"config.json unreadable: {e}"
        eng = cls(root, config_json, repos_cfg, v2_cfg, agents_md)
        eng._db = db
        eng._worklog = worklog
        return eng, agents_err

    # ---- 仓库车道 ----

    def repo_lane(self, repo: str) -> str:
        entry = (self.repos_cfg.get("repositories") or {}).get(repo)
        if entry and entry.get("lane"):
            return entry["lane"]
        return "unknown"

    def repo_entry(self, repo: str) -> dict:
        return (self.repos_cfg.get("repositories") or {}).get(repo) or {}

    # ---- 新鲜度 ----

    def check_freshness(self, created_at: str,
                        now: dt.datetime | None = None) -> Decision:
        """龄 <2h 的新鲜 issue 战略放弃（Playbook §0.9）。created_at 为 GitHub ISO 时间。"""
        try:
            created = dt.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return Decision(False, f"unparseable created_at: {created_at!r}", "freshness")
        now = now or dt.datetime.now(dt.timezone.utc)
        age_h = (now - created).total_seconds() / 3600
        if age_h < self.min_issue_age_hours:
            return Decision(False, f"issue age {age_h:.1f}h < {self.min_issue_age_hours}h, skip",
                            "freshness")
        return Decision(True, f"issue age {age_h:.1f}h ok", "freshness")

    # ---- 授权 ----

    def authorize(self, action: str, repo: str, *,
                  assigned_to_me: bool = False,
                  maintainer_confirmed: bool = False,
                  blocker_registry=None,
                  claim_check: bool = True) -> Decision:
        """中央写动作授权（计划 §10：policy_authorize(action, repo, candidate)）。

        fail-closed：任何条件不满足都拒绝并给出原因；不满足即停，不做"尽量允许"。
        """
        d = Decision(False, "", action=action, repo=repo)
        if action in ALWAYS_DENIED:
            d.reason = f"{action} is permanently denied by Playbook"
            return d
        if action not in WRITE_ACTIONS:
            d.reason = f"unknown action {action!r} not in whitelist"
            return d
        if not self.agents_md_ok:
            d.reason = "AGENTS.md missing/unparseable — all writes stopped (fail-closed)"
            return d
        lane = self.repo_lane(repo)
        if lane == "unknown":
            d.reason = f"unknown repo {repo}: {self.unknown_action}"
            return d
        if lane == "observe":
            d.reason = "observe-only repo, no writes"
            return d
        if action == "open_pr" and lane == "gated":
            d.reason = ("gated repo: comment analysis + await assignment first, "
                        "no direct PR (Playbook §2 闸门池专属)")
            return d
        if action == "open_pr" and lane == "restricted" and not maintainer_confirmed:
            d.reason = "restricted repo: only maintainer-confirmed candidates"
            return d
        if blocker_registry is not None and action in {"push", "open_pr", "comment_analysis"}:
            key = f"{repo}:{action}"
            if blocker_registry.is_blocked(key):
                d.reason = f"403 blocker registered for {key} — no retry this run"
                return d
        if action == "claim" and claim_check and not self.claim_slot_available():
            d.reason = "daily new-claim limit already used (worklog/ledger)"
            return d
        if action == "workflow_dispatch" and not assigned_to_me and lane == "gated":
            d.reason = "gated repo without assignment: cannot dispatch"
            return d
        d.allowed = True
        d.reason = "ok"
        return d

    def claim_slot_available(self) -> bool:
        """双闸：worklog 当日已有 claim/awaiting_assignment，或 SQLite 当日已占额 → 不可用。"""
        if self._worklog is not None:
            if self._worklog.claimed_today(self.tz_name):
                return False
        if self._db is not None:
            from .state_store import business_day
            day = business_day(dt.datetime.now(), self.tz_name)
            row = self._db._conn.execute(
                "SELECT candidate_id FROM claim_days WHERE day=?", (day,)).fetchone()
            if row is not None:
                return False
        return True

    # ---- 评论模板合规 ----

    @staticmethod
    def check_comment_text(text: str) -> Decision:
        """对外评论的底线校验：禁止求指派文案（Playbook §2）。"""
        if FORBIDDEN_PHRASES.search(text or ""):
            return Decision(False, "forbidden 'please assign me' style phrase", "comment")
        return Decision(True, "ok", "comment")

    # ---- 授权快照 ----

    def snapshot_id(self) -> str:
        """policy SHA：AGENTS.md + 仓库策略的 hash。变化 ⇒ 旧授权失效需重审。"""
        h = hashlib.sha256()
        h.update(self.agents_md_text.encode("utf-8"))
        h.update(str(_canonical(self.repos_cfg)).encode("utf-8"))
        h.update(str(_canonical(self.v2_cfg)).encode("utf-8"))
        return h.hexdigest()[:16]


def _load_yaml(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return None


def json_load(path: Path) -> dict:
    import json
    return json.loads(path.read_text(encoding="utf-8"))


def _canonical(obj) -> str:
    import json
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)
