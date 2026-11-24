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

## Read-only edge API

The existing `GH_TOKEN` and `INGEST_TOKEN` Worker secrets are reused. The controller automatically discovers the account workers.dev subdomain using its existing CF API token (Workers Scripts Read or Write), or accepts `CF_EDGE_URL=https://oss-scout-hunter.<subdomain>.workers.dev`. Ensure workers.dev is enabled for this Worker. `edge_reads_enabled: false` restores local reads; old or unavailable Workers temporarily fall back to local reads. A rate limit is never immediately bypassed with a local retry. Both API endpoints require the existing ingest token, and neither can publish or execute code.

- `POST /internal/v1/issue-context`: complete issue, comments, timeline; three independent fetches in parallel, bounded pagination; controller retains mechanical and AI rules.
- `POST /internal/v1/pr-snapshot`: full/delta GraphQL pagination for xixi-box; complete results only, controller retains the incremental merge and publication ledger.

Each invocation is bounded to 36 external subrequests, below the Workers Free limit. Deploy via the existing worker-deploy workflow before switching the controller. If the GitHub token does not identify xixi-box, PR retrieval falls back to the existing controller path; no incomplete snapshot can prune Feishu rows.

## Durable ingress

The `INGRESS` SQLite Durable Object stages issue versions before acknowledging scan delivery; its alarm sends up to 20 issues per controller receipt and retries failed deliveries. Old KV pending keys continue to drain via the existing VPS recovery path. No candidate/PR admission state is moved in this release. Deploy with Wrangler 4 and preserve the DO binding and migration tag on future revisions.


### Screening evidence offload

The Worker also fetches repository policies at an immutable default-branch SHA (including inherited community files after confirmed 404s), performs bounded competing-PR searches, and reads tracked prerequisite issue/PR facts. The VPS preserves the exact Python multilingual intent, repository policy, AI verdict and publication gates. Cross-repository references outside the roster and independent Actions publication checks remain direct reads. No GitHub write token or model credential is moved into these routes.
