"""只读：为什么候选没被消费。打印额度、drain 输入、每条的卡点。"""
import json, os, sqlite3, datetime as dt
from pathlib import Path

root = Path(os.environ.get("OSSCOUT_DASHBOARD_ROOT", "/data"))
print("=== now ===", dt.datetime.now().isoformat(timespec="seconds"))

print("\n=== claim_days (today) ===")
db = root / "state" / "scout_v2.sqlite3"
try:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = list(con.execute("SELECT day, candidate_id, ts FROM claim_days ORDER BY ts DESC LIMIT 10"))
    for r in rows:
        print("  ", r)
    print("  total today:", sum(1 for r in rows if r[0] == dt.datetime.now().strftime("%Y-%m-%d")))
    con.close()
except Exception as e:
    print("  db:", str(e)[:150])

print("\n=== queue status counts ===")
q = json.loads((root / "queue" / "candidates.json").read_text(encoding="utf-8"))
from collections import Counter
print("  total:", len(q))
print("  ", dict(Counter((v or {}).get("status") for v in q.values())))

print("\n=== eligible? (drain 的 ACTIVE + usable) ===")
ACTIVE = ("new", "queued", "queued_hist", "recheck_failed", "staged")
elig = []
blocked = Counter()
for k, e in q.items():
    e = e or {}
    if e.get("status") not in ACTIVE:
        continue
    if e.get("repo") in ("xixi-box/oss-scout-v2-test",):
        blocked["internal_repo"] += 1
        continue
    if e.get("blockers"):
        blocked["has_blockers"] += 1
        continue
    v = e.get("ai_verdict") or {}
    usable = v.get("usable")
    if usable is None:
        usable = (e.get("ai_grade") or {}).get("grade") == "A"
    if usable is not True:
        blocked["ai_not_usable"] += 1
        continue
    elig.append(k)
print("  eligible:", len(elig))
print("  blocked:", dict(blocked))
for k in elig[:12]:
    e = q[k]
    print("   ", k, "|", e.get("status"), "| staged_at:", e.get("staged_at", "-"),
          "|", (e.get("title") or "")[:40])

print("\n=== auto_repair / dispatch audit (last 2h) ===")
cut = (dt.datetime.now() - dt.timedelta(hours=2)).isoformat(timespec="seconds")
lines = (root / "state" / "worklog.jsonl").read_text(encoding="utf-8", errors="replace").splitlines()
recent = []
for ln in lines:
    if '"ts"' not in ln:
        continue
    try:
        r = json.loads(ln)
    except Exception:
        continue
    if str(r.get("ts", "")) >= cut and str(r.get("action")) in (
            "dashboard_auto_repair", "auto_repair_error", "stage",
            "dashboard_auto_repair_kickoff", "dashboard_staged_flush"):
        recent.append(r)
print("  count:", len(recent))
for r in recent[-8:]:
    print("   ", r["ts"], r["action"], r["target"], str(r.get("result"))[:110])

print("\n=== auto_repair (any action, last 10 today) ===")
try:
    hits = [l for l in lines if today in l and '"action": "dashboard_auto_repair"' in l]
    print("  auto_repair today:", len(hits))
    for l in hits[-10:]:
        r = json.loads(l)
        print("   ", r["ts"], "|", r.get("target"), "|", str(r.get("result"))[:100])
except Exception as e:
    print("  ", str(e)[:150])
