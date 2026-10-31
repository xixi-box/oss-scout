#!/bin/sh
set -e
mkdir -p /data/state /data/queue /data/artifacts /data/state/runs /data/configs

# 配置播种：镜像里的 configs.seed 是**权威版本**，部署新镜像就应该生效。
#
# 2026-10-30 实录：这里原本是"文件不存在才拷"，于是 /data/configs 保留了首次
# 部署那天的快照，之后改池子（173→40）、改每日上限（1→0→2）、删掉禁用 AI 的
# 仓库，全都没有到生产——流水线跑了一整天用的还是老配置，而且是无声的。
# 现在改成：只要内容与 seed 不同就覆盖。配置里没有运行期可写状态（状态都在
# /data/state 与 /data/queue），所以覆盖是安全的。
for f in /app/configs.seed/*; do
  [ -e "$f" ] || continue
  name=$(basename "$f")
  dst="/data/configs/$name"
  if [ ! -e "$dst" ]; then
    cp "$f" "$dst" 2>/dev/null || echo "[entrypoint] WARN: cannot seed $name; continuing"
  elif ! cmp -s "$f" "$dst"; then
    cp "$f" "$dst" 2>/dev/null && echo "[entrypoint] updated $name from image seed" \
      || echo "[entrypoint] WARN: cannot update $name; continuing"
  fi
done

# AGENTS.md：PolicyEngine fail-closed 依赖它，缺失会导致所有仓库 lane=unknown
if [ ! -e /data/AGENTS.md ] || ! cmp -s /app/AGENTS.md /data/AGENTS.md; then
  cp /app/AGENTS.md /data/AGENTS.md 2>/dev/null \
    || echo "[entrypoint] WARN: cannot seed AGENTS.md (permission)"
fi
if [ ! -e /data/config.json ] || ! cmp -s /app/config.json /data/config.json; then
  cp /app/config.json /data/config.json 2>/dev/null || true
fi

[ -f /data/queue/candidates.json ] || echo '{}' > /data/queue/candidates.json
[ -f /data/state/seen.json ] || echo '{}' > /data/state/seen.json

exec python -m uvicorn dashboard.app:app --host 0.0.0.0 --port 8787
