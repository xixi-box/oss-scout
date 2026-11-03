"""池子里全部仓库的两个核心指标（只读，人工复核用）。

指标定义：
  外部合并率 = fork 来源的 merged PR / 全部 merged PR（最近 100 个已关闭）
  中位合并耗时 = 外部 PR 从 created 到 merged 的小时数中位数
  外部 PR/周  = 外部 merged PR / 时间跨度 * 7

综合得分见 scout_v2/repo_score.py。

用法：python scripts/pool_rates.py [--json]
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scout_v2.repo_score import grade, total  # noqa: E402


def gh(*args, timeout=120):
    r = subprocess.run(["gh", *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    return r.stdout if r.returncode == 0 else ""


def rate(repo: str) -> dict:
    raw = gh("api", f"repos/{repo}/pulls?state=closed&per_page=100"
                    f"&sort=updated&direction=desc")
    try:
        prs = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return {"repo": repo, "error": "fetch"}
    merged = [p for p in prs if p.get("merged_at")]
    if not merged:
        return {"repo": repo, "error": "none"}

    times = sorted(datetime.fromisoformat(p["merged_at"].replace("Z", "+00:00"))
                   for p in merged)
    span = max((times[-1] - times[0]).total_seconds() / 86400, 1)

    def from_fork(p: dict) -> bool:
        full = str(((p.get("head") or {}).get("repo") or {})
                   .get("full_name") or "").lower()
        return bool(full) and full != repo.lower()

    ext = [p for p in merged if from_fork(p)]
    lat = []
    for p in ext[:40]:
        try:
            a = datetime.fromisoformat(p["created_at"].replace("Z", "+00:00"))
            b = datetime.fromisoformat(p["merged_at"].replace("Z", "+00:00"))
            lat.append((b - a).total_seconds() / 3600)
        except (ValueError, KeyError):
            continue
    lat.sort()
    med = lat[len(lat) // 2] if lat else None
    share = round(len(ext) / len(merged) * 100)
    per_week = round(len(ext) / span * 7, 1)
    return {"repo": repo, "ext_share": share, "ext_per_week": per_week,
            "median_h": round(med, 1) if med is not None else None,
            "score": total(share, med, per_week)}


def main() -> int:
    cfg = yaml.safe_load((ROOT / "configs" / "repositories.yaml")
                         .read_text(encoding="utf-8"))
    pool = [r for r in cfg["repositories"] if not r.startswith("xixi-box/")]

    rows = []
    for repo in pool:
        d = rate(repo)
        rows.append(d)
        if "error" in d:
            print(f"  {repo:42s} ERROR {d['error']}", file=sys.stderr)
        time.sleep(0.3)

    if "--json" in sys.argv:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0

    ok = [r for r in rows if "error" not in r]
    ok.sort(key=lambda r: -r["score"])
    print(f"\n| 仓库 | 总分 | 档 | 外部合并率 | 外部/周 | 中位合并耗时 |")
    print("|---|---|---|---|---|---|")
    for r in ok:
        h = f'{r["median_h"]}h' if r["median_h"] is not None else "—"
        print(f'| {r["repo"]} | {r["score"]} | {grade(r["score"])} | '
              f'{r["ext_share"]}% | {r["ext_per_week"]} | {h} |')
    for r in rows:
        if "error" in r:
            print(f'| {r["repo"]} | — | — | 数据不足 | — | — |')
    print(f"\n共 {len(rows)} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
