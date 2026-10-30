"""用最原始的 SQL 验证 BEGIN IMMEDIATE 是否真能防住并发重复插入。"""
import multiprocessing as mp
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DDL = ("CREATE TABLE IF NOT EXISTS claim_days ("
       " day TEXT NOT NULL, candidate_id TEXT NOT NULL,"
       " action TEXT NOT NULL, ts TEXT NOT NULL,"
       " PRIMARY KEY (day, candidate_id))")


def worker(db: str, q) -> None:
    c = sqlite3.connect(db, timeout=30, isolation_level=None)
    c.execute("PRAGMA journal_mode=WAL")
    try:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT candidate_id FROM claim_days WHERE day=?",
                        ("D",)).fetchone()
        if row is not None:
            c.execute("ROLLBACK")
            q.put(("no", row[0]))
            return
        c.execute("INSERT INTO claim_days VALUES (?,?,?,?)", ("D", "x#1", "claim", "t"))
        c.execute("COMMIT")
        q.put(("YES", ""))
    except Exception as e:  # noqa: BLE001
        try:
            c.execute("ROLLBACK")
        except Exception:  # noqa: BLE001
            pass
        q.put(("EXC", f"{type(e).__name__}: {e}"))
    finally:
        c.close()


def main() -> int:
    bad = 0
    with tempfile.TemporaryDirectory() as td:
        for i in range(30):
            db = str(Path(td) / f"r{i}.sqlite3")
            c = sqlite3.connect(db, isolation_level=None)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute(DDL)
            c.close()
            q = mp.Queue()
            ps = [mp.Process(target=worker, args=(db, q)) for _ in range(4)]
            for p in ps:
                p.start()
            for p in ps:
                p.join(30)
            res = [q.get(timeout=30) for _ in ps]
            wins = sum(1 for k, _ in res if k == "YES")
            if wins != 1:
                bad += 1
                print(f"round {i}: wins={wins} {res}")
        print(f"\n{bad}/30 rounds had wrong winner count")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
