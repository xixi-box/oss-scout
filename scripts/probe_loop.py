"""一次性探针：dashboard 循环为什么没写审计（只读）。

用法（容器内）：python /app/scripts/probe_loop.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

WORKLOG = Path("/data/state/worklog.jsonl")


def main() -> int:
    print("gh on PATH:", bool(shutil.which("gh")))
    wl = WORKLOG.read_text(encoding="utf-8", errors="replace").splitlines()
    print("worklog lines:", len(wl))
    print("--- last 3 of any kind ---")
    for ln in wl[-3:]:
        print(" ", ln[:160])
    dash = [ln for ln in wl if "dashboard_" in ln]
    print("dashboard_ audit count:", len(dash))

    spec_path = Path("/app/scripts/monitor_prs.py")
    if not spec_path.exists():
        print("monitor_prs missing at /app/scripts")
        return 0
    sys.path.insert(0, "/app")
    import importlib.util
    spec = importlib.util.spec_from_file_location("monitor_prs", spec_path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    try:
        prs = m.my_open_prs()
        print("open prs seen:", len(prs))
        if prs:
            print("sample:", prs[0]["repository"]["nameWithOwner"])
    except Exception as e:  # noqa: BLE001
        print("my_open_prs raised:", type(e).__name__, str(e)[:200])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
