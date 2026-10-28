"""列出候选池里带 AI 评级的条目（排队情况一目了然）。"""
import json
from collections import Counter

d = json.load(open("/data/queue/candidates.json", encoding="utf-8"))
print("total:", len(d))
print("status:", Counter(v.get("status") for v in d.values()))
print()

graded = []
for k, v in d.items():
    ai = v.get("ai_grade") or {}
    if not ai:
        continue
    graded.append((ai.get("grade"), v.get("status"), k,
                   (v.get("title") or "")[:64]))

graded.sort(key=lambda x: (x[0] or "Z", x[1] or ""))
print("=== graded candidates ===")
for g, st, k, t in graded:
    print("%-3s %-22s %-42s %s" % (g, st, k, t))
print()
print("A-level total:", sum(1 for x in graded if x[0] == "A"))
print("A-level still new:",
      sum(1 for x in graded if x[0] == "A" and x[1] == "new"))
