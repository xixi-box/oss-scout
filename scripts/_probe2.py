import json
q = json.load(open('/data/queue/candidates.json', encoding='utf-8'))
# 今天开的 5 个 PR 对应的 issue 号
want = ["bytedance/deer-flow#7821", "qdrant/qdrant#11360",
        "bytedance/deer-flow#7811", "milvus-io/milvus#54965",
        "agentgateway/agentgateway#4219"]
for w in want:
    v = q.get(w)
    if v is None:
        print(f"{w}: NOT IN QUEUE")
    else:
        print(f"{w}: status={v.get('status')} discovered={v.get('discovered_at')}")
# 看 claim_days 全表（不只是最近12条）
import sqlite3
conn = sqlite3.connect('/data/state/scout_v2.sqlite3')
rows = conn.execute("SELECT day, COUNT(*) FROM claim_days GROUP BY day ORDER BY day DESC").fetchall()
print("--- claim_days per day ---")
for d, n in rows:
    print(f"  {d}: {n}")
conn.close()
