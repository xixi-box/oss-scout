#!/usr/bin/env python
"""P6-C：Shadow Run 每日统计（只读采集，绝不触发任何外部写）。

从真实状态文件采集当日指标并落盘：
  state/shadow/YYYY-MM-DD.json   当日快照
  state/shadow/history.jsonl     追加历史（Dashboard/报表消费）
  state/runs/shadow_YYYY-MM-DD.md 当日报告

指标：发现量、有效候选率、Bug 线索与复现、Modal 计量、OpenCode 失败分类、
重复候选比例、任务耗时、health 规则识别、资源（psutil 缺失则"未知"）。
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SHADOW = ROOT / "state" / "shadow"
TODAY = dt.date.today().isoformat()


def load_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def candidates_metrics() -> dict:
    q = load_json(ROOT / "queue" / "candidates.json", {})
    today_new = [v for v in q.values()
                 if str(v.get("discovered_at", "")).startswith(TODAY)]
    ab = sum(1 for v in today_new if v.get("class") in ("A", "B"))
    dupes = sum(1 for v in today_new
                if "duplicate" in json.dumps(v.get("signals", {})).lower()
                or "duplicate" in json.dumps(v.get("blockers", [])).lower())
    return {"candidates_total": len(q), "discovered_today": len(today_new),
            "ab_today": ab, "effective_rate": (round(ab / len(today_new), 2) if today_new else None),
            "duplicate_flagged_today": dupes}


def worklog_metrics() -> dict:
    actions: Counter = Counter()
    errors = 0
    lines = 0
    p = ROOT / "state" / "worklog.jsonl"
    if p.exists():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            lines += 1
            if str(rec.get("ts", "")).startswith(TODAY):
                actions[rec.get("action", "?")] += 1
                blob = json.dumps(rec, ensure_ascii=False).lower()
                if any(m in blob for m in ("fail", "error", "403", "blocker")):
                    errors += 1
    return {"worklog_lines": lines, "today_actions": dict(actions.most_common(12)),
            "today_error_lines": errors}


def bughunt_metrics() -> dict:
    s = load_json(ROOT / "state" / "bughunt" / "p6_summary.json", None)
    if not s:
        return {"scanned_repos": 0, "findings": 0, "opencode_analyzed": 0,
                "opencode_false_positives": 0}
    findings = sum(r.get("findings", 0) for r in s.get("repos", []))
    analyzed = [r.get("opencode_analysis") or {} for r in s.get("repos", [])]
    fps = sum(1 for a in analyzed if "false positive" in str(a.get("analysis", "")).lower())
    dur = None
    try:
        t0 = dt.datetime.fromisoformat(s["started"])
        t1 = dt.datetime.fromisoformat(s["finished"])
        dur = round((t1 - t0).total_seconds() / 60, 1)
    except (KeyError, ValueError):
        pass
    return {"scanned_repos": len(s.get("repos", [])), "findings": findings,
            "opencode_analyzed": len(analyzed), "opencode_false_positives": fps,
            "reproduced_bugs": 0,  # 尚无隔离复现任务跑在真实线索上
            "total_minutes": dur}


def modal_metrics() -> dict:
    try:
        from scout_v2.budget import ModalCliBilling
        b = ModalCliBilling()
        metered = b.metered_usage_usd()
        return {"metered_usd": metered, "available": metered is not None}
    except Exception:  # noqa: BLE001
        return {"metered_usd": None, "available": False}


def resource_metrics() -> dict:
    try:
        import psutil  # type: ignore
        vm = psutil.virtual_memory()
        return {"ram_total_mb": int(vm.total / 1e6), "ram_used_pct": vm.percent,
                "cpu_pct": psutil.cpu_percent(interval=1)}
    except Exception:  # noqa: BLE001
        return {"ram_total_mb": "未知", "ram_used_pct": "未知", "cpu_pct": "未知"}


def policy_metrics() -> dict:
    h = load_json(ROOT / "state" / "health.json", {})
    repos = h.get("repos", {})
    flagged = sum(1 for r in repos.values() if (r.get("policy") or {}).get("flags"))
    stale = False
    if h.get("computed_at"):
        age = (dt.datetime.now() - dt.datetime.fromisoformat(h["computed_at"])).days
        stale = age > 7
    return {"health_repos": len(repos), "policy_flagged": flagged, "health_stale": stale}


def main() -> int:
    SHADOW.mkdir(parents=True, exist_ok=True)
    snap = {
        "date": TODAY,
        "candidates": candidates_metrics(),
        "worklog": worklog_metrics(),
        "bughunt": bughunt_metrics(),
        "modal": modal_metrics(),
        "resources": resource_metrics(),
        "policy": policy_metrics(),
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    (SHADOW / f"{TODAY}.json").write_text(
        json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
    with (SHADOW / "history.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(snap, ensure_ascii=False) + "\n")

    c, b = snap["candidates"], snap["bughunt"]
    report = [
        f"# Shadow Run 日报 {TODAY}", "",
        f"- 候选池总量：{c['candidates_total']}；今日新增：{c['discovered_today']}"
        f"（A/B {c['ab_today']}，有效率 {c['effective_rate']}）",
        f"- Bug 线索：{b['findings']}（{b['scanned_repos']} 仓）；OpenCode 分析 "
        f"{b['opencode_analyzed']}，判误报 {b['opencode_false_positives']}；已复现真实 Bug：{b['reproduced_bugs']}",
        f"- Modal 计量：{snap['modal']['metered_usd']}（可用性 {snap['modal']['available']}）",
        f"- worklog 今日动作：{snap['worklog']['today_actions']}；错误相关行：{snap['worklog']['today_error_lines']}",
        f"- 资源：{snap['resources']}",
        f"- health：{snap['policy']}（stale={snap['policy']['health_stale']}）",
        "", "> 只读采集，未执行任何外部写操作。", "",
    ]
    (ROOT / "state" / "runs" / f"shadow_{TODAY}.md").write_text(
        "\n".join(report), encoding="utf-8")
    print(json.dumps(snap, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
