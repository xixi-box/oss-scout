"""只读：过去 10 小时的自动开 PR 与凌晨消费状态。"""
import json, os, datetime as dt
from pathlib import Path

root = Path(os.environ.get("OSSCOUT_DASHBOARD_ROOT", "/data"))
now = dt.datetime.now()
cut = (now - dt.timedelta(hours=10)).isoformat(timespec="seconds")

print("=== now:", now.isoformat(timespec="seconds"), "===")

print("\n=== worklog auto_repair / claim / pr_open (last 10h) ===")
try:
    lines = (root / "state" / "worklog.jsonl") \
        .read_text(encoding="utf-8", errors="replace").splitlines()
except OSError as e:
    print(" read failed:", str(e)[:120]); raise SystemExit(0)
recent = []
for ln in lines:
    if '"ts"' not in ln:
        continue
    try:
        r = json.loads(ln)
    except Exception:
        continue
    ts = str(r.get("ts") or "")
    if ts >= cut and str(r.get("action")) in (
            "auto_repair", "auto_repair_error", "claim", "pipeline_pr_open",
            "pipeline_abort", "staged_flush", "pipeline_claim",
            "dashboard_auto_repair", "dashboard_staged_flush"):
        recent.append(r)
print(" count:", len(recent))
for r in recent[-20:]:
    print("  ", r["ts"], "|", str(r.get("action")).replace("dashboard_", ""),
          "|", r.get("target"), "|", str(r.get("result"))[:110])

print("\n=== queue: new/staged/aborted_competed counts ===")
q = json.loads((root / "queue" / "candidates.json").read_text(encoding="utf-8"))
from collections import Counter
print(" ", dict(Counter((v or {}).get("status") for v in q.values())))

print("\n=== claim_days today ===")
import sqlite3
try:
    con = sqlite3.connect(f"file:{root/'state'/'scout_v2.sqlite3'}?mode=ro", uri=True)
    today = now.strftime("%Y-%m-%d")
    for r in con.execute("SELECT candidate_id, ts FROM claim_days WHERE day=? ORDER BY ts", (today,)):
        print("  ", r)
    con.close()
except Exception as e:
    print("  db:", str(e)[:150])

print("\n=== worklog mtime (loop heartbeat) ===")
st = (root / "state" / "worklog.jsonl").stat()
print("  mtime:", dt.datetime.fromtimestamp(st.st_mtime).isoformat(),
      "| age:", round(now.timestamp() - st.st_mtime, 1), "s")
