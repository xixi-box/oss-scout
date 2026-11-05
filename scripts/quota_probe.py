"""只读：打印今日认领额度与候选消费状态。"""
import json, os, sqlite3
from pathlib import Path

root = Path(os.environ.get("OSSCOUT_DASHBOARD_ROOT", "/data"))
db = root / "state" / "scout_v2.sqlite3"
print("=== claim_days ===")
try:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    for row in con.execute("SELECT * FROM claim_days ORDER BY day DESC LIMIT 5"):
        print(" ", row)
    print(" tables:", [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")])
    con.close()
except Exception as e:
    print(" db:", str(e)[:150])

print("=== queue status counts ===")
try:
    q = json.loads((root / "queue" / "candidates.json").read_text(encoding="utf-8"))
    from collections import Counter
    print(" total:", len(q))
    print(" ", dict(Counter((v or {}).get("status") for v in q.values())))
except Exception as e:
    print(" q:", str(e)[:150])

print("=== auto_repair audit today ===")
import datetime as dt
today = dt.datetime.now().strftime("%Y-%m-%d")
try:
    lines = (root / "state" / "worklog.jsonl").read_text(
        encoding="utf-8", errors="replace").splitlines()
    hits = [l for l in lines if today in l and "auto_repair" in l]
    print(" count:", len(hits))
    for l in hits[-6:]:
        try:
            r = json.loads(l)
            print(" ", r["ts"], r["action"], r["target"], str(r.get("result"))[:120])
        except Exception:
            pass
except Exception as e:
    print(" wl:", str(e)[:150])
