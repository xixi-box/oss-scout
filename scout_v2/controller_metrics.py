"""Bounded controller timings. Operation names are fixed; no payloads or secrets."""
from contextlib import contextmanager
import json
import threading
import time


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
            try:
                with (root / "state/worklog.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            except OSError:
                pass
