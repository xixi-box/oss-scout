"""看最近被 ingest 拒掉的候选及原因，判断 AI 判定是否过严。"""
import json
from collections import Counter

s = json.load(open("/data/state/seen.json", encoding="utf-8"))
print("总记录:", len(s))
print(dict(Counter(v.get("status") for v in s.values() if isinstance(v, dict))))
print()

rejected = [(k, v) for k, v in s.items()
            if isinstance(v, dict) and v.get("status") in ("rejected", "blocked")]
print("=== 最近被拒的 12 条 ===")
for k, v in rejected[-12:]:
    why = (v.get("why") or "-")[:110]
    print("%-44s %-9s %s" % (k, v.get("status"), why))
