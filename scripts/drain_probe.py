"""只读：打印 auto_repair 循环的环境开关与心跳。"""
import json, os
print("OSSCOUT_DISABLE_AUTO_REPAIR =", repr(os.environ.get("OSSCOUT_DISABLE_AUTO_REPAIR")))
print("OSSCOUT_DASHBOARD_ROOT =", repr(os.environ.get("OSSCOUT_DASHBOARD_ROOT")))
# 循环在 app 内创建，从外面看不到线程；改看 worklog mtime 有没有心跳
import datetime as dt
from pathlib import Path
wl = Path(os.environ.get("OSSCOUT_DASHBOARD_ROOT", "/data")) / "state" / "worklog.jsonl"
st = wl.stat()
print("worklog mtime:", dt.datetime.fromtimestamp(st.st_mtime).isoformat(),
      "| size:", st.st_size)
import time
print("now:", dt.datetime.now().isoformat(timespec="seconds"))
lines = wl.read_text(encoding="utf-8", errors="replace").splitlines()
print("last 3:")
for ln in lines[-3:]:
    try:
        r = json.loads(ln)
        print("  ", r.get("ts"), r.get("action"), r.get("target"))
    except Exception:
        print("  ", ln[:120])
