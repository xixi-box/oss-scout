"""Bounded controller timings. Operation names are fixed; no payloads or secrets."""
from contextlib import contextmanager
import json
import threading
import time
from pathlib import Path


@contextmanager
def timed_operation(root, operation):
    started, cpu = time.monotonic(), time.thread_time()
    try:
        yield
    finally:
        elapsed = time.monotonic() - started
        if elapsed >= 2:
            from .state_store import now_local_iso
            record = {"ts": now_local_iso(), "action": "controller_slow_operation",
                      "target": operation, "source": "controller",
                      "result": {"elapsed_seconds": round(elapsed, 3),
                                 "cpu_seconds": round(time.thread_time() - cpu, 3),
                                 "thread": threading.current_thread().name}}
            # A long wall time with little CPU is often paging/descheduling,
            # not a slow JSON encoder. Record bounded, non-sensitive evidence.
            try:
                status = Path("/proc/self/status").read_text()
                for line in status.splitlines():
                    if line.startswith(("VmRSS:", "VmSwap:")):
                        key, value, *_ = line.split()
                        record["result"]["rss_kib" if key == "VmRSS:" else "swap_kib"] = int(value)
            except (OSError, ValueError):
                pass
            try:
                with (root / "state/worklog.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            except OSError:
                pass
