"""复现 ClaimLedger 并发认领的偶发失败，打印每个进程的实际返回值。"""
import multiprocessing as mp
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scout_v2.state_store import ClaimLedger

TZ = "Asia/Shanghai"


def worker(db: str, q) -> None:
    try:
        ledger = ClaimLedger(Path(db))
        ok, why = ledger.try_claim(TZ, "aaif-goose/goose#1", max_per_day=0)
        ledger.close()
        q.put((ok, why))
    except Exception as e:  # noqa: BLE001
        q.put((None, f"EXC {type(e).__name__}: {e}"))


def one_round(td: str, idx: int) -> int:
    db = str(Path(td) / f"l{idx}.sqlite3")
    ClaimLedger(Path(db)).close()
    q = mp.Queue()
    ps = [mp.Process(target=worker, args=(db, q)) for _ in range(4)]
    for p in ps:
        p.start()
    for p in ps:
        p.join(30)
    res = [q.get(timeout=30) for _ in ps]
    wins = sum(1 for ok, _ in res if ok)
    if wins != 1:
        print(f"round {idx}: FAIL wins={wins}")
        for ok, why in res:
            print(f"   ok={ok!r} why={why}")
    return wins


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        bad = 0
        for i in range(20):
            if one_round(td, i) != 1:
                bad += 1
        print(f"\n{bad}/20 rounds failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
