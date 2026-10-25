#!/bin/sh
set -e
if [ ! -d /data/configs ]; then
  cp -r /app/configs.seed /data/configs
  echo "[entrypoint] seeded configs"
fi
[ -f /data/AGENTS.md ] || cp /app/AGENTS.md /data/AGENTS.md
mkdir -p /data/state /data/queue /data/artifacts /data/state/runs
[ -f /data/queue/candidates.json ] || echo '{}' > /data/queue/candidates.json
[ -f /data/state/seen.json ] || echo '{}' > /data/state/seen.json
exec python -m uvicorn dashboard.app:app --host 0.0.0.0 --port 8787
