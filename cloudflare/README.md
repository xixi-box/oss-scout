# Cloudflare Issue Hunter（KV 拉取模式，无需隧道）

你只需要创建两样东西：
1. API Token：https://dash.cloudflare.com/profile/api-tokens → 用 "Edit Cloudflare Workers" 模板
2. Account ID：https://dash.cloudflare.com 首页右侧（或 Workers 页）→ 复制

创建好发我，其余我来跑。

## 我这边执行的（供参考）

```
npx wrangler kv namespace create SEEN          # CLOUDFLARE_API_TOKEN 环境变量已设
npx wrangler secret put GH_TOKEN               # GitHub 只读 token
npx wrangler deploy
```

## 链接

- API Token：https://dash.cloudflare.com/profile/api-tokens
- Workers 面板：https://dash.cloudflare.com/?to=/:account/workers
