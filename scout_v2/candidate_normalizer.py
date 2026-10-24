"""候选规范化：旧 queue/candidates.json 条目 ↔ v2 Candidate（计划 §4.1）。

旧 id 形如 "owner/repo#N"（issue 车道）；v2 candidate_id 为
"github:owner/repo:issue:N"。bug 车道不伪造 issue_number。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .schemas import Candidate

LEGACY_ID = re.compile(r"^(?P<repo>[^#]+)#(?P<number>\d+)$")

LEGACY_LANE_MAP = {"open": "issue", "gated": "issue", "restricted": "issue", "issue": "issue"}


def legacy_to_candidate(entry: dict[str, Any]) -> Candidate:
    m = LEGACY_ID.match(entry.get("id", ""))
    repo = entry.get("repo") or (m.group("repo") if m else "")
    number = entry.get("number") or (int(m.group("number")) if m else None)
    lane_in = entry.get("lane", "open")
    return Candidate(
        candidate_id=f"github:{repo}:issue:{number}",
        lane=LEGACY_LANE_MAP.get(lane_in, "issue"),
        repo=repo,
        issue_number=number,
        title=entry.get("title", ""),
        url=entry.get("url", ""),
        grade=entry.get("class", ""),
        score=entry.get("score"),
        status=entry.get("status", "discovered"),
        first_seen_at=entry.get("discovered_at", ""),
        updated_at=entry.get("discovered_at", ""),
        legacy={k: v for k, v in entry.items()
                if k not in {"id", "repo", "number", "title", "url", "class",
                             "score", "status", "discovered_at"}},
    )


def candidate_to_legacy(c: Candidate) -> dict[str, Any]:
    """v2 → 旧结构：discover.py / queue 兼容视图。只对 issue 车道有意义。"""
    if c.lane != "issue" or c.issue_number is None:
        raise ValueError(f"candidate {c.candidate_id} is not issue-lane; no legacy mapping")
    out = {
        "id": f"{c.repo}#{c.issue_number}",
        "repo": c.repo,
        "number": c.issue_number,
        "title": c.title,
        "url": c.url,
        "lane": "open",
        "status": c.status,
    }
    if c.score is not None:
        out["score"] = c.score
        out["class"] = c.grade
    out.update(c.legacy)
    return out


def load_legacy_queue(path) -> dict[str, Candidate]:
    import json
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {k: legacy_to_candidate(v) for k, v in data.items()}
