"""数据契约：Candidate / Job / Result（计划 §4）。

与旧结构的兼容约定：
- 旧 queue 条目的 `id` 形如 "owner/repo#N"，v2 的 `candidate_id` 形如
  "github:owner/repo:issue:N"（bug 车道为 "github:owner/repo:bug:<sha>:<fingerprint>"）。
- 旧字段（repo/number/title/url/lane/score/class/status/lease）原样保留在 Candidate 上，
  candidate_normalizer 负责双向映射；不再为 bug 车道伪造 issue_number。
- 写 JSON 一律经 state_store.atomic_json_write（临时文件 + 校验 + .bak）。
"""
from __future__ import annotations

import dataclasses
import uuid
from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = 1

# Job.mode 的合法值；broad_scan 只允许走 Modal（job_router 强制）
MODES = {"scan", "reproduce", "repair", "review", "ci"}
EXECUTION_SCOPES = {"broad_scan", "project_contribution"}
BACKENDS = {"modal", "github_actions", "local"}
OUTCOMES = {"succeeded", "inconclusive", "failed", "blocked", "timed_out"}


@dataclass
class Candidate:
    candidate_id: str
    lane: str                       # issue | bug
    repo: str                       # owner/name
    issue_number: int | None
    title: str = ""
    url: str = ""
    grade: str = ""                 # A | B | C（旧 class）
    score: int | None = None
    base_sha: str | None = None
    repro_status: str = "unverified"
    competition_status: str = "unknown"
    policy_status: str = "pending"
    status: str = "discovered"
    first_seen_at: str = ""
    updated_at: str = ""
    evidence_paths: list[str] = field(default_factory=list)
    # 旧结构透传字段（signals/blockers/url 等），normalizer 写入
    legacy: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Candidate":
        known = {f.name for f in dataclasses.fields(cls)}
        legacy = {k: v for k, v in d.items() if k not in known}
        base = {k: v for k, v in d.items() if k in known}
        return cls(**base, legacy=legacy)


@dataclass
class Job:
    schema_version: int = SCHEMA_VERSION
    job_id: str = field(default_factory=lambda: f"job-{uuid.uuid4()}")
    idempotency_key: str = ""
    candidate_id: str = ""
    repo: str = ""
    base_sha: str = ""
    mode: str = ""
    execution_scope: str = "project_contribution"
    policy_snapshot_id: str = ""
    timeout_seconds: int = 1800
    cpu: int = 2
    memory_mb: int = 4096
    max_estimated_compute_usd: float = 0.0
    model_policy: str = "free_first"
    allow_github_write: bool = False    # 固定 False；控制器才是唯一写者
    created_at: str = ""
    # P2 可选扩展（旧 Job JSON 不含这些字段也能解析）
    test_cmd: list[str] = field(default_factory=list)   # reproduce/repair：验证命令
    ruleset: str = ""                                    # scan：semgrep 规则集
    context: str = ""                                    # repair：issue 上下文（截断后进 prompt）

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Job":
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})

    def validate(self) -> list[str]:
        errs: list[str] = []
        if self.mode not in MODES:
            errs.append(f"invalid mode: {self.mode!r}")
        if self.execution_scope not in EXECUTION_SCOPES:
            errs.append(f"invalid execution_scope: {self.execution_scope!r}")
        if self.allow_github_write:
            errs.append("allow_github_write must be false for any worker job")
        if not self.idempotency_key:
            errs.append("idempotency_key required")
        if self.mode in {"reproduce", "repair", "review", "ci"} and not self.base_sha:
            errs.append(f"mode {self.mode} requires full base_sha")
        return errs


@dataclass
class Result:
    schema_version: int = SCHEMA_VERSION
    job_id: str = ""
    backend: str = ""
    outcome: str = "inconclusive"
    reproduced_on_base: bool = False
    fixed_on_patch: bool = False
    regression_tests_passed: bool = False
    patch_path: str | None = None
    test_report_path: str | None = None
    logs_path: str | None = None
    base_sha: str = ""
    model_used: str | None = None
    measured_cost_usd: float | None = None
    unverified_items: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Result":
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})
