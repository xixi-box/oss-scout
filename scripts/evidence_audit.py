"""审计 evidence chain 失败的分布（只读）。

aborted_no_evidence 是候选池里最大的异常群（46+ 条），但 mark_candidate 当时
没有把失败原因写进候选条目，只有 worklog 里留了文本。这个脚本解析 worklog，
按 (repro, fixed) 组合、gate 错误和仓库分布统计，判断是系统性缺陷还是随机失败。

实测格式两种（2026-10-31）：
  旧: evidence chain incomplete: failed repro=True fixed=False gate=PASS; claim released
  新: ... gate=['reason'] patch=no; claim released
正则要同时吃下这两种。

用法（容器内）：
  python /app/scripts/evidence_audit.py            # 统计
  python /app/scripts/evidence_audit.py --raw 3    # 打印原始样本行
"""
from __future__ import annotations

import collections
import json
import re
import sys

# 实测格式（2026-10-31）：'evidence chain incomplete: failed repro=True
# fixed=False gate=PASS; claim released'——outcome 的值直接跟在冒号后，
# 没有 "outcome=" 字面量。
REASON_RE = re.compile(r"incomplete: (\S+) repro=(\S+) fixed=(\S+) gate=(.*?)(?:;|$)")


def _rows():
    for line in open("/data/state/worklog.jsonl", encoding="utf-8",
                     errors="replace"):
        try:
            r = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if r.get("action") == "pipeline_abort":
            yield r


def main() -> int:
    if "--raw" in sys.argv:
        want = 3
        if len(sys.argv) > sys.argv.index("--raw") + 1:
            want = int(sys.argv[sys.argv.index("--raw") + 1])
        shown = 0
        for r in _rows():
            if "evidence chain" in str(r.get("result", "")):
                print(repr(str(r.get("result"))[:220]))
                shown += 1
                if shown >= want:
                    break
        return 0

    combos: collections.Counter = collections.Counter()
    gates: collections.Counter = collections.Counter()
    by_repo: collections.Counter = collections.Counter()
    total_abort = 0
    n = 0
    for r in _rows():
        total_abort += 1
        res = str(r.get("result", ""))
        if "evidence chain incomplete" not in res:
            continue
        n += 1
        m = REASON_RE.search(res)
        if m:
            outcome, repro, fixed, gate = m.groups()
            combos[(outcome, repro, fixed)] += 1
            g = gate.strip()
            if g and g != "PASS":
                gates[g[:80]] += 1
        target = str(r.get("target", ""))
        repo = target.split("#")[0] if "#" in target else "?"
        by_repo[repo] += 1

    print(f"pipeline_abort total: {total_abort}")
    print(f"evidence failures: {n}")
    print("--- (outcome, repro, fixed) combos ---")
    for k, v in combos.most_common(10):
        print(f"  {k}  {v}")
    print("--- gate reasons (top) ---")
    for k, v in gates.most_common(8):
        print(f"  {v:4d}  {k}")
    print("--- by repo ---")
    for k, v in by_repo.most_common(12):
        print(f"  {k:40s} {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
