"""审计 evidence chain 失败的分布（只读）。

46 条 aborted_no_evidence 是候选池里最大的异常群，但 mark_candidate 当时没有把
失败原因写进候选条目，只有 worklog 里留了文本。这个脚本解析 worklog，按
(repro, fixed, patch) 组合和仓库分布统计，用来判断是系统性缺陷还是随机失败。

用法（容器内）：python /app/scripts/evidence_audit.py
"""
from __future__ import annotations

import collections
import json
import re

REASON_RE = re.compile(
    r"outcome=(\S+) repro=(\S+) fixed=(\S+) gate=(.*?) patch=(\S+)$")


def main() -> int:
    import sys
    if "--raw" in sys.argv:
        # 打印原始样本行，用于核对格式
        want = int(sys.argv[sys.argv.index("--raw") + 1]) if len(sys.argv) > sys.argv.index("--raw") + 1 else 3
        shown = 0
        for line in open("/data/state/worklog.jsonl", encoding="utf-8", errors="replace"):
            try:
                r = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if r.get("action") == "pipeline_abort" and "evidence chain" in str(r.get("result", "")):
                print(repr(str(r.get("result"))[:220]))
                shown += 1
                if shown >= want:
                    break
        return 0
    reasons: collections.Counter = collections.Counter()
    by_repo: collections.Counter = collections.Counter()
    total_abort = 0
    n = 0
    for line in open("/data/state/worklog.jsonl", encoding="utf-8",
                     errors="replace"):
        try:
            r = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if r.get("action") != "pipeline_abort":
            continue
        total_abort += 1
        res = str(r.get("result", ""))
        if "evidence chain incomplete" not in res:
            continue
        n += 1
        m = REASON_RE.search(res)
        if m:
            reasons[(m.group(2), m.group(3), m.group(5))] += 1
        target = str(r.get("target", ""))
        repo = target.split("#")[0] if "#" in target else "?"
        by_repo[repo] += 1

    print(f"pipeline_abort total: {total_abort}")
    print(f"evidence failures: {n}")
    print("--- (repro, fixed, patch) combos ---")
    for k, v in reasons.most_common(10):
        print(f"  {k}  {v}")
    print("--- by repo ---")
    for k, v in by_repo.most_common(12):
        print(f"  {k:40s} {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
