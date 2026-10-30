"""统计每个仓库对外部贡献者的友好程度。

    python scripts/audit_contrib_friendliness.py [--limit N]

对每个仓库取最近 N 个**已关闭**的 PR，统计：
- 外部贡献者（author_association == "NONE"）提交了多少
- 其中被合并的比例  ← 最重要的指标：外部人能不能合进去
- 拒绝率

判据说明：
外部贡献者能合并 ≠ 任何人都能合，但它是所有可观测信号里最接近"这个仓库
愿意接受外人改动"的。比起 star 数、活跃度，它直接回答"我们来提 PR 有用吗"。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def gh(args: list[str], timeout: int = 90) -> tuple[int, str]:
    p = subprocess.run(["gh"] + args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    return p.returncode, (p.stdout or "")


def stats(repo: str, limit: int) -> dict | None:
    try:
        rc, out = gh(["api",
                      f"repos/{repo}/pulls?state=closed&per_page={limit}&sort=updated&direction=desc",
                      "--jq", "[.[] | {assoc: .author_association, merged: (.merged_at != null)}]"],
                     timeout=120)
    except subprocess.TimeoutExpired:
        return None
    if rc != 0:
        return None
    try:
        items = json.loads(out or "[]")
    except json.JSONDecodeError:
        return None
    if not items:
        return None

    # 外部贡献者 = NONE（没合并过任何东西）+ CONTRIBUTOR（合并过但非成员）
    # + FIRST_TIME_CONTRIBUTOR。注意 GitHub 的 author_association 是动态的：
    # 一个人第一次被合并后会从 FIRST_TIME_CONTRIBUTOR 升级成 CONTRIBUTOR，
    # 所以只看 FIRST_TIME_CONTRIBUTOR 永远是 0（2026-10-30 实测 6 个仓库全为 0）。
    EXT = {"NONE", "CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR"}
    ext = [i for i in items if i.get("assoc") in EXT]
    ext_merged = [i for i in ext if i.get("merged")]
    all_merged = [i for i in items if i.get("merged")]
    return {
        "repo": repo,
        "sampled": len(items),
        "ext": len(ext),
        "ext_merged": len(ext_merged),
        # 外部贡献者提交的 PR 里，最终被合并的比例
        "ext_merge_rate": round(len(ext_merged) / len(ext), 3) if ext else None,
        # 外部合并占全部合并的比例
        "ext_share_of_merges": round(len(ext_merged) / len(all_merged), 3) if all_merged else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=60)
    ap.add_argument("--pool", default=str(ROOT / "configs" / "scan_pool_wide.txt"))
    ap.add_argument("--out", default=str(ROOT / "state" / "friendliness.json"))
    args = ap.parse_args()

    pool = [l.strip() for l in Path(args.pool).read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.startswith("#")]

    out = []
    for i, repo in enumerate(pool, 1):
        rec = stats(repo, args.limit)
        if rec:
            out.append(rec)
            rate = rec["ext_merge_rate"]
            rate_s = f"{rate:.0%}" if rate is not None else "n/a"
            print(f"[{i}/{len(pool)}] {repo:44s} ext={rec['ext']:2d} "
                  f"merged={rec['ext_merged']:2d} rate={rate_s}", flush=True)
        else:
            print(f"[{i}/{len(pool)}] {repo:44s} SKIP (no data)", flush=True)

    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    print(f"\nsaved: {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
