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


def opts_cy(spy_cls):
    """把一个只实现 score_issue 的 Spy 包装成 GoClient 兼容构造器。"""
    class _Client:
        def __init__(self, **kwargs):
            self._spy = spy_cls()

        def score_issue(self, issue):
            return self._spy.score_issue(issue)

    return _Client


def _env(min_age: float = 2):
    tmp = Path(tempfile.mkdtemp())
    (tmp / "configs").mkdir(parents=True)
    (tmp / "configs" / "repositories.yaml").write_text(yaml.dump({
        "defaults": {"min_issue_age_hours": min_age},
        "repositories": {"a/b": {"lane": "open"}}}), encoding="utf-8")
    (tmp / "configs" / "cloudflare.yaml").write_text(
        yaml.dump({"ingest_token": "t"}), encoding="utf-8")
    (tmp / "configs" / "scorer.yaml").write_text(
        yaml.dump({"api_key": "test-key", "model": "test-model"}),
        encoding="utf-8")
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


def test_fresh_issue_still_reaches_the_ai():
    """年龄不再是硬闸（用户裁定 2026-11-05）：太新鲜的 issue 照常走 AI 判定，
    年龄与"报告者想自己修"作为信号交给模型权衡。"""
    ds, tmp = _env()
    now = dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    called = []

    class Spy:
        def __init__(self, **k):
            pass

        def score_issue(self, issue):
            called.append(issue)
            return {"usable": True, "reason": "ok", "model": "x"}

    import scout_v2.workers.issue_scorer as isc
    orig = isc.GoClient
    isc.GoClient = opts_cy(Spy)
    try:
        n, _ = D.ingest_cloudflare_issues(ds, [_item(now)])
    finally:
        isc.GoClient = orig
    assert called, "fresh issue must still be scored by AI"
    # 年龄已从输入删除（用户裁定 2026-11-06）：不该再出现 age 信号
    assert "age_hours" not in called[0]
    seen = json.loads((tmp / "state" / "seen.json").read_text(encoding="utf-8"))
    assert seen.get("a/b#99", {}).get("status") != "too_fresh"


def test_reporter_offer_is_detected_and_passed_to_ai():
    """报告者说"让我来修"→ 作为信号传给 AI，让它判 NO（礼貌让位）。"""
    ds, tmp = _env()
    now = dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    item = _item(now)
    item["body_excerpt"] += "\n\nMay I prepare that focused fix?"
    called = []

    class Spy:
        def __init__(self, **k):
            pass

        def score_issue(self, issue):
            called.append(issue)
            return {"usable": True, "reason": "ok", "model": "x"}

    import scout_v2.workers.issue_scorer as isc
    orig = isc.GoClient
    isc.GoClient = opts_cy(Spy)
    try:
        D.ingest_cloudflare_issues(ds, [item])
    finally:
        isc.GoClient = orig
    assert called[0]["reporter_offered_fix"] is True


def test_body_ai_warning_reaches_the_scorer_prompt():
    """clickhouse#126287 实录：body 写 "the fix maybe is not trivial to ask AI
    to do it"——命中的句子必须以 body signal 进 comments_block，让模型判 NO。"""
    ds, tmp = _env()
    now = dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    item = _item(now)
    item["body_excerpt"] += ("\n\nThe fix maybe is not trivial to ask AI to do "
                             "it, so a maintainer should look at it first.")
    called = []

    class Spy:
        def __init__(self, **k):
            pass

        def score_issue(self, issue):
            called.append(issue)
            return {"usable": False, "reason": "warned off AI", "model": "x"}

    import scout_v2.workers.issue_scorer as isc
    orig = isc.GoClient
    isc.GoClient = opts_cy(Spy)
    try:
        D.ingest_cloudflare_issues(ds, [item])
    finally:
        isc.GoClient = orig
    issue = called[0]
    assert issue["claimish_signal"] is True
    assert any("not trivial to ask AI" in str(c.get("body"))
               for c in issue.get("comments") or [])

    # 端到端再验一次 prompt 组装：真 GoClient + 假 chat，捕获 prompt
    prompts = []

    def fake_chat(prompt, max_tokens=200):
        prompts.append(prompt)
        return True, "DECISION: NO\nREASON: maintainer says not for AI"

    client = isc.GoClient(key="k", model="m")
    client.chat = fake_chat
    verdict = client.score_issue(issue)
    assert verdict["usable"] is False
    assert "not trivial to ask AI" in prompts[0]
    assert "Comments (recent)" in prompts[0]
    assert "body signal" in prompts[0]
    # NO 清单硬条款在模板里
    assert "not trivial for" in isc.SCORE_PROMPT


def test_reason_columns_are_split():
    """粗筛原因（规则否决）与细筛原因（AI 理由）分开记。"""
    ds, tmp = _env()
    billing = _item("2026-11-01T00:00:00Z")
    billing["title"] = "[BUG] please refund my subscription"
    n, _ = D.ingest_cloudflare_issues(ds, [billing])
    seen = json.loads((tmp / "state" / "seen.json").read_text(encoding="utf-8"))
    e = seen.get("a/b#99", {})
    assert e.get("status") == "blocked"
    assert e.get("coarse_reason"), e
    assert "why" not in e, "旧单列原因应迁到 coarse/fine"


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
