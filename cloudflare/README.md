# Cloudflare Issue Hunter

Worker 每分钟轮询 GitHub issues → 回传本地 `/api/cloudflare/ingest` 入队。

## 你需要创建/提供的东西（创建好发我即可）

1. **GitHub Token**（只读公开仓库即可）：
   https://github.com/settings/tokens/new — 勾 `public_repo` 就够
2. **Ingest Token**：随便一串随机字符（我可以帮你生成）
3. **Ingest URL**：Worker 能访问到你本机 `:8787` 的地址（需要 Tailscale/隧道，见下）

## 部署（本机装了 wrangler 后）

```bash
npx wrangler login          # 浏览器授权
npx wrangler kv namespace create SEEN   # 把输出的 id 填进 wrangler.toml
npx wrangler secret put GH_TOKEN
npx wrangler secret put INGEST_TOKEN
npx wrangler secret put INGEST_URL
npx wrangler deploy
```

## 链接

- Cloudflare 控制台：https://dash.cloudflare.com
- Workers & Pages：https://dash.cloudflare.com/?to=/:account/workers
- KV：https://dash.cloudflare.com/?to=/:account/workers/kv
- GitHub token：https://github.com/settings/tokens
