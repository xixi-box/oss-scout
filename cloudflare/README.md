# Cloudflare Issue Hunter 部署

用户裁定（2026-10-25）：Issue Hunter 从 VPS 剥离，由 Cloudflare Worker 每分钟轮询。

## 架构

```
CF Worker (cron 每分钟)
  ├─ GitHub REST: repos/{repo}/issues?since=<水位>   （core 限额 5000/h，够用；不用 search API 的 30/min 限制）
  ├─ 预筛：open、无 assignee、非 PR、排除标签
  └─ POST 本地 /api/cloudflare/ingest（X-Ingest-Token 认证）
        └─ 本地 discover 层同款评分 → queue/candidates.json（A/B 入队，C/未知只记 seen）
```

原 discover.py 全量扫描保留作兜底（每 2h 观测任务不变）；Worker 是快速泳道。

## 部署步骤

1. `cd cloudflare && cp wrangler.toml <你的配置>`，填 KV namespace id：
   `npx wrangler kv namespace create SEEN`
2. 配三个 secret：
   - `npx wrangler secret put GH_TOKEN`（GitHub PAT，public_repo 读权限即可）
   - `npx wrangler secret put INGEST_URL`（如 `http://<主机>:8787/api/cloudflare/ingest`；
     本机在 NAT 后时用 Tailscale/隧道地址——不要把无 TLS 的端点直接暴露公网）
   - `npx wrangler secret put INGEST_TOKEN`（与 Dashboard「额度页 → Cloudflare」里设置的
     ingest token 一致）
3. `npx wrangler deploy`
4. 回到 Dashboard 额度页 → Cloudflare 卡片，填同样的 ingest token 和仓库列表保存。

## 费用

- Workers 免费档 10 万请求/天：每分钟 1 次 ≈ 1440 次/天，余量充足。
- GitHub core API 5000 次/小时：每分钟 ~14 个仓库各 1 次列表调用，余量充足。
- 不产生任何付费资源；KV 免费档（1k 写/天）足够水位更新。

## 安全

- INGEST_TOKEN 是唯一回传凭证；本地 ingest 端点校验失败一律 403。
- Worker 只读公开 issue 数据，无任何 GitHub 写权限（token 用最小 scope）。
