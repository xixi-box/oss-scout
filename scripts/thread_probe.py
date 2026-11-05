"""只读：检查 dashboard 进程线程是否还活着（cardio = worklog 心跳）。"""
import os, json, time, datetime as dt
from pathlib import Path

root = Path(os.environ.get("OSSCOUT_DASHBOARD_ROOT", "/data"))
wl = root / "state" / "worklog.jsonl"
st = wl.stat()
now = time.time()
age = now - st.st_mtime
print("worklog age_seconds:", round(age, 1))
print("verdict:", "ALIVE" if age < 120 else ("STALLED" if age < 3600 else "DEAD"))

# 容器内 ps：python 线程数（/proc/1/task 计数）
try:
    n = len(os.listdir("/proc/1/task"))
    print("pid1 threads:", n)
except Exception as e:
    print("proc:", e)

# 每个 loop 应有独立线程；如果线程数骤减说明线程死了
