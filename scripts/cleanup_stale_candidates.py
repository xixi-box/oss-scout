"""清掉候选池里永远不会被派发的条目。

2026-10-30 实录：池子从 173 换成 40 之后，/data 里还留着 127 条老仓库的候选。
它们的状态是 new/queued，但 drain_ai_graded 只派发 ai_verdict.usable is True 的
条目，而这些是 AI 细筛接上之前入队的（usable 全 None），于是永久滞留在池子里
当噪声：看板上的计数、每轮 drain 的扫描都被它们污染。

只删「仓库已不在池内」，以及在池内但压根没有 AI 判定、且已超过保留期的条目。
在池内且判定为 usable=True 的一律保留——那是待修复的真候选。

用法（容器内）：python scripts/cleanup_stale_candidates.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

import yaml


def _pool(root: Path) -> set[str]:
    """读池子。优先镜像里的 configs.seed：/data/configs 可能是旧快照。

    2026-10-30 实录：/data/configs 由 entrypoint 首次部署时播种，之后不再更新，
    导致清理脚本以为池子还是 41 个仓库。判断"哪些候选该删"必须用当前代码里的
    池子定义，不能用可能过期的运行期副本。
    """
    for path in (Path("/app/configs.seed/repositories.yaml"),
                 root / "configs" / "repositories.yaml",
                 Path(__file__).resolve().parents[1] / "configs" / "repositories.yaml"):
        if path.exists():
            cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            print(f"[cleanup] pool from {path}")
            return set((cfg.get("repositories") or {}).keys())
    raise SystemExit("no repositories.yaml found")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.environ.get("OSSCOUT_DASHBOARD_ROOT", "/data"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    root = Path(args.root)
    qpath = root / "queue" / "candidates.json"
    if not qpath.exists():
        print(f"no candidates at {qpath}", file=sys.stderr)
        return 1
    pool = _pool(root)
    q = json.loads(qpath.read_text(encoding="utf-8"))
    before = len(q)

    ACTIVE = ("new", "queued", "queued_hist", "recheck_failed")
    drop_out_of_pool = [k for k, v in q.items()
                        if v.get("status") in ACTIVE and v.get("repo") not in pool]
    drop_no_verdict = [k for k, v in q.items()
                       if v.get("status") in ACTIVE
                       and v.get("repo") in pool
                       and (v.get("ai_verdict") or {}).get("usable") is None
                       and (v.get("ai_grade") or {}).get("grade") is None]
    # needs_invite 是已删除的 PR 门禁留下的状态：2026-10-30 删掉 pr_invited()
    # 之后，代码里再没有地方读这个状态，也没有地方写它——它们既不会被派发，
    # 也不会被重新判定，纯粹是永久滞留。直接删掉（原始 issue 还在 GitHub 上，
    # 真要找回来按 id 重新拉即可）。
    drop_needs_invite = [k for k, v in q.items()
                         if v.get("status") == "needs_invite"]
    # 暂存超期的也在这里清一次，避免 drain 只是"看不见"而不是"删掉"
    drop_expired_stage: list[str] = []
    for k, v in q.items():
        if v.get("status") != "staged":
            continue
        ts = str(v.get("staged_at") or v.get("discovered_at") or "")
        try:
            import datetime as _dt
            then = _dt.datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
            if (_dt.datetime.now() - then).days > 3:
                drop_expired_stage.append(k)
        except (ValueError, TypeError):
            drop_expired_stage.append(k)      # 没有时间戳的暂存无法判龄，按过期处理

    print(f"pool={len(pool)} candidates={before}")
    counts: dict[str, int] = {}
    for v in q.values():
        counts[str(v.get("status"))] = counts.get(str(v.get("status")), 0) + 1
    for st, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {st:24s} {n}")
    print(f"drop (repo not in pool): {len(drop_out_of_pool)}")
    print(f"drop (in pool, never AI-judged): {len(drop_no_verdict)}")
    print(f"drop (needs_invite, gate removed): {len(drop_needs_invite)}")
    print(f"drop (staged > 3 days): {len(drop_expired_stage)}")
    for k in (drop_out_of_pool + drop_no_verdict + drop_needs_invite
              + drop_expired_stage)[:10]:
        print(f"  - {k}")

    if args.dry_run:
        print("dry run, nothing written")
        return 0

    for k in (drop_out_of_pool + drop_no_verdict + drop_needs_invite
              + drop_expired_stage):
        q.pop(k, None)
    fd, tmp = tempfile.mkstemp(dir=str(qpath.parent), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(q, f, ensure_ascii=False, indent=1)
    os.replace(tmp, qpath)
    print(f"wrote {qpath}: {before} -> {len(q)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
