"""只读：查当日认领记录 vs 配置上限，以及今天实际开了哪些 PR。"""
import json
import sqlite3
from pathlib import Path

DB = Path("/data/state/scout_v2.sqlite3")
WL = Path("/data/state/worklog.jsonl")
CFG = Path("/data/configs/repositories.yaml")


def main() -> int:
    print("=== configured cap ===")
    for line in CFG.read_text(encoding="utf-8").splitlines():
        if "max_new_claims" in line:
            print(" ", line.strip())

    print("\n=== claim_days (recent 15) ===")
    conn = sqlite3.connect(str(DB))
    try:
        rows = conn.execute(
            "SELECT day, candidate_id, action, ts FROM claim_days "
            "ORDER BY ts DESC LIMIT 15").fetchall()
        for r in rows:
            print("  ", r)
    finally:
        conn.close()

    print("\n=== today's pipeline_* events ===")
    today = "2026-11-03"
    n = 0
    for line in WL.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            r = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        ts = str(r.get("ts", ""))
        if not ts.startswith(today):
            continue
        act = r.get("action", "")
        if act in ("claim",) or "pr_open" in act or "stage" in act:
            print(f"  {ts} {act} {r.get('target')} :: {str(r.get('result'))[:80]}")
            n += 1
    print(f"  (matched {n})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
