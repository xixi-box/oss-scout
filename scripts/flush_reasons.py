"""只读：打印凌晨批量处理踢出的候选及原因分布。"""
import json, os
from pathlib import Path
from collections import Counter

root = Path(os.environ.get("OSSCOUT_DASHBOARD_ROOT", "/data"))
q = json.loads((root / "queue" / "candidates.json").read_text(encoding="utf-8"))
morning = [(k, e) for k, e in q.items()
           if str((e or {}).get("abort_reason") or "").startswith("morning")]
print("morning-kicked total:", len(morning))
reasons = Counter()
for k, e in morning:
    reason = str((e or {}).get("abort_reason") or "")[:150]
    reasons[reason[:80]] += 1
    print(f"  {k} | {reason}")
print("\n=== 原因分布 ===")
for r, n in reasons.most_common(10):
    print(f"  x{n}: {r}")
