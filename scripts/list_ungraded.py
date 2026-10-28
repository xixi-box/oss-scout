"""列出未评级的 new 候选（供扩大范围前先看现有库存）。"""
import json
from collections import Counter

d = json.load(open("/data/queue/candidates.json", encoding="utf-8"))

ungraded = [(k, v) for k, v in d.items()
            if v.get("status") == "new" and not (v.get("ai_grade") or {})]

print("ungraded new:", len(ungraded))
print("by repo:", Counter(v.get("repo", k.split("#")[0]) for k, v in ungraded))
print()
for k, v in ungraded[:40]:
    print("%-42s %s" % (k, (v.get("title") or "")[:70]))
