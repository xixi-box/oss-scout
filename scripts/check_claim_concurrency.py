"""验证 ClaimLedger 在多进程下不会重复认领同一候选。"""
import multiprocessing as mp
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scout_v2.state_store import ClaimLedger


def worker(db: str, cid: str, q) -> None:
    ledger = ClaimLedger(Path(db))
    try:
        ok, why = ledger.try_claim("Asia/Shanghai", cid, max_per_day=0)
        q.put((ok, why))
    finally:
        ledger.close()


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        db = str(Path(td) / "l.sqlite3")
        ClaimLedger(Path(db)).close()
        q = mp.Queue()
        ps = [mp.Process(target=worker, args=(db, "a/b#1", q)) for _ in range(4)]
        for p in ps:
            p.start()
        for p in ps:
            p.join(30)
        res = [q.get(timeout=10) for _ in ps]
        for ok, why in res:
            print(("OK  " if ok else "no  ") + why)
        print("ok count:", sum(1 for ok, _ in res if ok), "(expected 1)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
