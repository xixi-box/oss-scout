#!/bin/sh
# 统一 worker 入口：唯一职责是执行 job，产出 result.json + artifacts。
# 安全约定：
#   - 本容器不应注入任何 GitHub 写令牌（检查到即拒绝启动）。
#   - 不读宿主 ~/.git-credentials、~/.netrc。
set -eu
if [ -n "${GH_TOKEN:-}" ] || [ -n "${GITHUB_TOKEN:-}" ]; then
  echo "[entrypoint] refuse: workers must not hold GitHub tokens" >&2
  exit 2
fi
JOB="${1:-/workspace/job.json}"
OUT="${2:-/workspace/artifacts}"
# 兼容 job-b64 + stdout 信封交付协议
if [ -n "${OSSCOUT_JOB_B64:-}" ]; then
  exec python -m scout_v2.workers.core --job-b64 "$OSSCOUT_JOB_B64" --out "$OUT" \
    --repo-dir /workspace/repo --emit-envelope
fi
exec python -m scout_v2.workers.core --job "$JOB" --out "$OUT"
