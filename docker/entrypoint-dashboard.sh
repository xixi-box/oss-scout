#!/bin/sh
set -e
mkdir -p /data/state /data/queue /data/artifacts /data/state/runs /data/configs

# 逐文件补种（幂等）：比"整目录不存在才拷"更稳——手工建过目录时不会漏种子
for f in /app/configs.seed/*; do
  [ -e "$f" ] || continue
  name=$(basename "$f")
  if [ ! -e "/data/configs/$name" ]; then
    cp "$f" "/data/configs/$name" 2>/dev/null       || echo "[entrypoint] WARN: cannot seed $name (permission); continuing"
  fi
done

# AGENTS.md：PolicyEngine fail-closed 依赖它，缺失会导致所有仓库 lane=unknown
[ -f /data/AGENTS.md ] || cp /app/AGENTS.md /data/AGENTS.md 2>/dev/null   || echo "[entrypoint] WARN: cannot seed AGENTS.md (permission)"
[ -f /data/config.json ] || cp /app/config.json /data/config.json 2>/dev/null || true

[ -f /data/queue/candidates.json ] || echo '{}' > /data/queue/candidates.json
[ -f /data/state/seen.json ] || echo '{}' > /data/state/seen.json

exec python -m uvicorn dashboard.app:app --host 0.0.0.0 --port 8787
