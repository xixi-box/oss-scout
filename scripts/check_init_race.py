"""验证并发 __init__ 是否会让两个进程的插入都成功（唯一约束失效？）。"""
import multiprocessing as mp
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scout_v2.state_store import ClaimLedger


def worker(db: str, q) -> None:
    try:
        l = ClaimLedger(Path(db))
        l._conn.execute(
            "INSERT INTO claim_days VALUES ('D','x#1','claim','t')")
        n = l._conn.execute("SELECT COUNT(*) FROM claim_days").fetchone()[0]
        l.close()
        q.put(("ok", n))
    except Exception as e:  # noqa: BLE001
        q.put(("exc", f"{type(e).__name__}: {e}"))


def main() -> int:
    bad = 0
    with tempfile.TemporaryDirectory() as td:
        for r in range(10):
            db = str(Path(td) / f"t{r}.sqlite3")
            ClaimLedger(Path(db)).close()   # 先建表
            q = mp.Queue()
            ps = [mp.Process(target=worker, args=(db, q)) for _ in range(4)]
            for p in ps:
                p.start()
            for p in ps:
                p.join(20)
            res = [q.get(timeout=20) for _ in ps]
            oks = sum(1 for k, _ in res if k == "ok")
            c = sqlite3.connect(db, isolation_level=None)
            rows = c.execute("SELECT * FROM claim_days").fetchall()
            c.close()
            if oks != 1 or len(rows) != 1:
                bad += 1
                print(f"round {r}: oks={oks} rows={rows}")
                for k, v in res:
                    print(f"    {k}: {v}")
        print(f"\n{bad}/10 rounds wrong")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
