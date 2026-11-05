"""ingest 的年龄闸：太新鲜的 issue 不碰，且在 AI 评分之前拦截。

phoenix#17506/#17507 实录：issue 提出 4 分钟后我们的 PR 就开了，撞上
另一位先礼貌请求要修的用户，违反该仓库 CONTRIBUTING 的
wait-for-confirmation——factory bot 在 review 里公开点名。
"""
from __future__ import annotations

import json
import sys
import tempfile
import types
import datetime as dt
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dashboard.data as D  # noqa: E402


def _env(min_age: float = 2):
    tmp = Path(tempfile.mkdtemp())
    (tmp / "configs").mkdir(parents=True)
    (tmp / "configs" / "repositories.yaml").write_text(yaml.dump({
        "defaults": {"min_issue_age_hours": min_age},
        "repositories": {"a/b": {"lane": "open"}}}), encoding="utf-8")
    (tmp / "configs" / "cloudflare.yaml").write_text(
        yaml.dump({"ingest_token": "t"}), encoding="utf-8")
    (tmp / "queue").mkdir()
    (tmp / "state").mkdir()
    (tmp / "queue" / "candidates.json").write_text("{}", encoding="utf-8")
    (tmp / "state" / "seen.json").write_text("{}", encoding="utf-8")
    ds = types.SimpleNamespace(root=tmp, _cached=lambda c, t, f: f())
    return ds, tmp


def _item(created: str):
    return {"repo": "a/b", "number": 99,
            "title": "[BUG]: to_annotation_dataframe fails on zero rows",
            "body_excerpt": ("Steps:\n1. filter a scored df to zero rows\n\n"
                             "Expected: empty frame\nActual: ValueError"),
            "labels": [], "created_at": created, "url": "u"}


def test_fresh_issue_is_not_queued_and_is_marked_too_fresh():
    ds, tmp = _env()
    now = dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    n, _ = D.ingest_cloudflare_issues(ds, [_item(now)])
    assert n == 0
    seen = json.loads((tmp / "state" / "seen.json").read_text(encoding="utf-8"))
    e = seen.get("a/b#99", {})
    assert e.get("status") == "too_fresh"
    assert "why" in e


def test_gate_runs_before_the_ai_scorer():
    """太新鲜的 issue 不应该烧一次 AI 评分调用。"""
    ds, tmp = _env()
    now = dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    called = []

    class Spy:
        def __init__(self, **k):
            pass

        def score_issue(self, issue):
            called.append(issue)
            return {"usable": True, "reason": "", "model": "x"}

    import dashboard.data as _D
    orig = _D._discover_score
    _D._discover_score = lambda: type("D", (), {"RE_BILLING": orig().RE_BILLING,
                                                "RE_FEATURE_LABEL": orig().RE_FEATURE_LABEL,
                                                "RE_BAD_TITLE": orig().RE_BAD_TITLE})()
    try:
        n, _ = D.ingest_cloudflare_issues(ds, [_item(now)])
    finally:
        _D._discover_score = orig
    assert n == 0
    assert not called, "age gate must run before any AI call"


def test_old_enough_issue_still_flows_through():
    ds, tmp = _env()
    old = (dt.datetime.utcnow() - dt.timedelta(hours=6)) \
        .isoformat(timespec="seconds") + "Z"
    # body 做成会被 AI 判可修的形态；本地无 scorer key → 走到 verdict 分支
    n, _ = D.ingest_cloudflare_issues(ds, [_item(old)])
    seen = json.loads((tmp / "state" / "seen.json").read_text(encoding="utf-8"))
    status = seen.get("a/b#99", {}).get("status")
    assert status in ("queued", "rejected", "blocked"), status
    assert status != "too_fresh"
