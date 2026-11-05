"""清理 seen.json 里的 too_fresh 死档。

背景：早先粗筛用"issue 龄 < 2h"硬拦，被打回的直接标 too_fresh 不再评估。
用户裁定（2026-11-05）：年龄不是硬闸，交给 AI 判定时权衡——所以这批
记录应当重新获得评估机会，而不是躺在 seen.json 里永久挡住它们。

做法：删除这类记录（等价于"从未见过"），下一轮 Worker 再送来时会走
完整的粗筛 + AI 判定流程。只读统计后原子替换，不碰其他状态。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

root = Path(os.environ.get("OSSCOUT_DASHBOARD_ROOT", "/data"))
seen_path = root / "state" / "seen.json"

try:
    seen = json.loads(seen_path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as e:
    print(f"cannot read {seen_path}: {e}")
    raise SystemExit(1)

doomed = [k for k, v in seen.items()
          if str((v or {}).get("status")) == "too_fresh"]
# 纯太新鲜：重新评估；同时把"why"这类旧单列原因迁成新列名，避免丢信息
for k, v in seen.items():
    v = v or {}
    if "why" in v and "coarse_reason" not in v and "fine_reason" not in v:
        status = str(v.get("status"))
        col = "coarse_reason" if status in ("blocked", "unknown_repo",
                                            "too_fresh") else "fine_reason"
        v[col] = v.pop("why")

for k in doomed:
    seen.pop(k, None)

tmp = seen_path.with_suffix(".json.tmp")
tmp.write_text(json.dumps(seen, ensure_ascii=False), encoding="utf-8")
json.loads(tmp.read_text(encoding="utf-8"))
os.replace(tmp, seen_path)
print(f"purged too_fresh: {len(doomed)} | remaining: {len(seen)}")
