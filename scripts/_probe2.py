import json, collections
from pathlib import Path

q = json.load(open('/data/queue/candidates.json', encoding='utf-8'))
print("total:", len(q))
print("--- pr_open keys ---")
for k, v in q.items():
    if v.get('status') == 'pr_open':
        print(f"  {k}  repo={v.get('repo')}  nr={v.get('number')}")
print("--- new keys (first 12) ---")
n = 0
for k, v in q.items():
    if v.get('status') == 'new':
        print(f"  {k}  repo={v.get('repo')}  nr={v.get('number')}  discovered={v.get('discovered_at')}")
        n += 1
        if n >= 12:
            break
