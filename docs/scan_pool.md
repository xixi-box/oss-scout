# 扫描池仓库名单（173 个）

生成时间 2026-10-30，数据来自 `scripts/audit_pool.py` + `scripts/audit_pr_gate.py`。

全部分布在 `configs/repositories.yaml` 里，lane 统一 `open`；
唯一例外是会关闭未受邀 PR 的 4 个仓库标 `gated`。

## Python（60）

> 最易自动验证：pytest 生态统一，依赖装得快，agent 写测试成功率高

| 仓库 | stars | 最近提交 | 门禁 |
|---|---|---|---|
| `Significant-Gravitas/AutoGPT` | 187,826 | 2026-10-30 | 无 |
| `langchain-ai/langchain` | 148,706 | 2026-10-30 | 🔴 需先受邀 |
| `fastapi/fastapi` | 104,276 | 2026-10-27 | 无 |
| `pallets/flask` | 74,928 | 2026-10-07 | 无 |
| `ansible/ansible` | 71,158 | 2026-10-28 | 无 |
| `FoundationAgents/MetaGPT` | 71,084 | 2026-01-21 | 无 |
| `scikit-learn/scikit-learn` | 67,922 | 2026-10-29 | 无 |
| `scrapy/scrapy` | 65,106 | 2026-10-28 | 无 |
| `BerriAI/litellm` | 62,366 | 2026-10-30 | 无 |
| `microsoft/autogen` | 61,630 | 2026-10-28 | 无 |
| `Textualize/rich` | 57,675 | 2026-06-23 | 无 |
| `run-llama/llama_index` | 52,741 | 2026-10-26 | 无 |
| `pandas-dev/pandas` | 50,256 | 2026-10-30 | 无 |
| `apache/airflow` | 47,320 | 2026-10-30 | 无 |
| `langchain-ai/langgraph` | 44,149 | 2026-10-29 | 🔴 需先受邀 |
| `psf/black` | 42,073 | 2026-10-30 | 无 |
| `stanfordnlp/dspy` | 38,854 | 2026-10-29 | 无 |
| `Textualize/textual` | 37,549 | 2026-10-09 | 无 |
| `python-poetry/poetry` | 34,323 | 2026-10-28 | 无 |
| `numpy/numpy` | 33,197 | 2026-10-30 | 无 |
| `openai/openai-python` | 31,982 | 2026-10-30 | 无 |
| `tqdm/tqdm` | 31,372 | 2026-10-05 | 无 |
| `pydantic/pydantic` | 29,236 | 2026-10-30 | 🔴 需先受邀 |
| `deepset-ai/haystack` | 26,881 | 2026-10-30 | 无 |
| `PrefectHQ/prefect` | 24,101 | 2026-10-30 | 无 |
| `python/mypy` | 20,712 | 2026-10-29 | 无 |
| `fastapi/sqlmodel` | 18,505 | 2026-10-15 | 无 |
| `aio-libs/aiohttp` | 16,596 | 2026-10-30 | 无 |
| `dagster-io/dagster` | 16,354 | 2026-10-30 | 无 |
| `dottxt-ai/outlines` | 15,988 | 2026-09-21 | 无 |
| `treeverse/dvc` | 15,952 | 2026-10-26 | 无 |
| `encode/httpx` | 15,703 | 2026-10-02 | 无 |
| `pre-commit/pre-commit` | 15,672 | 2026-10-20 | 无 |
| `scipy/scipy` | 15,166 | 2026-10-30 | 无 |
| `microsoft/playwright-python` | 15,077 | 2026-10-29 | 无 |
| `pytest-dev/pytest` | 14,657 | 2026-10-30 | 无 |
| `567-labs/instructor` | 14,081 | 2026-10-26 | 无 |
| `dask/dask` | 13,937 | 2026-09-29 | 无 |
| `Kludex/starlette` | 12,697 | 2026-10-29 | 无 |
| `sqlalchemy/sqlalchemy` | 12,272 | 2026-10-29 | 🔴 需先受邀 |
| `fivetran/great_expectations` | 11,910 | 2026-10-30 | 无 |
| `pypa/pip` | 10,303 | 2026-10-29 | 无 |
| `MagicStack/asyncpg` | 8,117 | 2026-10-08 | 无 |
| `pypa/pip-tools` | 8,004 | 2026-10-26 | 无 |
| `python-attrs/attrs` | 5,863 | 2026-10-24 | 无 |
| `pypa/virtualenv` | 5,060 | 2026-10-28 | 无 |
| `unionai-oss/pandera` | 4,503 | 2026-10-30 | 无 |
| `googleapis/python-genai` | 4,049 | 2026-10-29 | 无 |
| `tox-dev/tox` | 3,979 | 2026-10-30 | 无 |
| `ansible/ansible-lint` | 3,928 | 2026-10-30 | 无 |
| `PyCQA/flake8` | 3,833 | 2026-10-28 | 无 |
| `python-arq/arq` | 3,018 | 2026-04-16 | 无 |
| `pypa/setuptools` | 2,872 | 2026-10-29 | 无 |
| `dateutil/dateutil` | 2,641 | 2026-10-25 | 无 |
| `qdrant/qdrant-client` | 1,373 | 2026-10-29 | 无 |
| `benjaminp/six` | 1,033 | 2026-02-23 | 无 |
| `frictionlessdata/frictionless-py` | 849 | 2026-10-15 | 无 |
| `mistralai/client-python` | 773 | 2026-10-28 | 无 |
| `cohere-ai/cohere-python` | 406 | 2026-10-27 | 无 |
| `weaviate/weaviate-python-client` | 229 | 2026-10-28 | 无 |

## Go（35）

> `go test` 简单直接，编译快，是自动化最友好的非 Python 生态

| 仓库 | stars | 最近提交 | 门禁 |
|---|---|---|---|
| `kubernetes/kubernetes` | 128,523 | 2026-10-30 | 无 |
| `microsoft/TypeScript` | 111,675 | 2026-10-30 | 无 |
| `gin-gonic/gin` | 89,450 | 2026-10-10 | 无 |
| `caddyserver/caddy` | 78,563 | 2026-10-30 | 无 |
| `moby/moby` | 72,222 | 2026-10-29 | 无 |
| `prometheus/prometheus` | 66,856 | 2026-10-30 | 无 |
| `traefik/traefik` | 65,416 | 2026-10-30 | 无 |
| `etcd-io/etcd` | 52,434 | 2026-10-28 | 无 |
| `hashicorp/terraform` | 50,089 | 2026-10-29 | 无 |
| `spf13/cobra` | 44,732 | 2026-07-11 | 无 |
| `gofiber/fiber` | 40,233 | 2026-10-30 | 无 |
| `evanw/esbuild` | 40,118 | 2026-08-09 | 无 |
| `go-gorm/gorm` | 39,987 | 2026-10-24 | 无 |
| `docker/compose` | 38,527 | 2026-10-30 | 无 |
| `istio/istio` | 38,479 | 2026-10-30 | 无 |
| `hashicorp/vault` | 36,448 | 2026-10-29 | 无 |
| `k3s-io/k3s` | 34,283 | 2026-10-29 | 无 |
| `labstack/echo` | 32,784 | 2026-10-30 | 无 |
| `spf13/viper` | 30,493 | 2026-01-12 | 无 |
| `helm/helm` | 30,385 | 2026-10-30 | 无 |
| `hashicorp/consul` | 30,096 | 2026-10-29 | 无 |
| `rancher/rancher` | 26,023 | 2026-10-30 | 无 |
| `pulumi/pulumi` | 25,833 | 2026-10-30 | 无 |
| `argoproj/argo-cd` | 24,503 | 2026-10-30 | 无 |
| `urfave/cli` | 24,306 | 2026-10-29 | 无 |
| `redis/go-redis` | 22,283 | 2026-10-30 | 无 |
| `containerd/containerd` | 21,449 | 2026-10-30 | 无 |
| `ent/ent` | 17,218 | 2026-10-14 | 无 |
| `argoproj/argo-workflows` | 17,069 | 2026-10-30 | 无 |
| `jackc/pgx` | 14,368 | 2026-10-27 | 无 |
| `crossplane/crossplane` | 12,208 | 2026-10-30 | 无 |
| `kubernetes-sigs/kustomize` | 12,191 | 2026-10-18 | 无 |
| `linkerd/linkerd2` | 11,521 | 2026-10-29 | 无 |
| `fluxcd/flux2` | 8,485 | 2026-10-26 | 无 |
| `gin-contrib/cors` | 2,001 | 2026-10-29 | 无 |

## Rust（30）

> `cargo test` 明确但首次编译 10 分钟以上，需要更长超时预算

| 仓库 | stars | 最近提交 | 门禁 |
|---|---|---|---|
| `rust-lang/rust` | 120,833 | 2026-10-30 | 无 |
| `tauri-apps/tauri` | 112,332 | 2026-10-29 | 无 |
| `denoland/deno` | 108,770 | 2026-10-29 | 无 |
| `oven-sh/bun` | 96,469 | 2026-10-30 | 无 |
| `astral-sh/uv` | 91,357 | 2026-10-30 | 无 |
| `BurntSushi/ripgrep` | 69,562 | 2026-10-27 | 无 |
| `alacritty/alacritty` | 66,085 | 2026-10-26 | 无 |
| `sharkdp/bat` | 60,976 | 2026-10-29 | 无 |
| `starship/starship` | 60,509 | 2026-10-29 | 无 |
| `astral-sh/ruff` | 50,337 | 2026-10-30 | 无 |
| `helix-editor/helix` | 46,734 | 2026-09-29 | 无 |
| `sharkdp/fd` | 44,903 | 2026-10-30 | 无 |
| `nushell/nushell` | 40,749 | 2026-10-29 | 无 |
| `ajeetdsouza/zoxide` | 40,364 | 2026-10-24 | 无 |
| `pola-rs/polars` | 40,233 | 2026-10-30 | 无 |
| `pnpm/pnpm` | 36,963 | 2026-10-30 | 无 |
| `zellij-org/zellij` | 35,890 | 2026-10-30 | 无 |
| `swc-project/swc` | 34,205 | 2026-10-30 | 无 |
| `tokio-rs/tokio` | 33,549 | 2026-10-29 | 无 |
| `dandavison/delta` | 32,607 | 2026-10-28 | 无 |
| `chroma-core/chroma` | 29,609 | 2026-10-28 | 无 |
| `wezterm/wezterm` | 29,396 | 2026-10-26 | 无 |
| `tokio-rs/axum` | 27,658 | 2026-10-30 | 无 |
| `biomejs/biome` | 26,022 | 2026-10-30 | 无 |
| `actix/actix-web` | 24,897 | 2026-10-29 | 无 |
| `clap-rs/clap` | 16,806 | 2026-10-06 | 无 |
| `rust-lang/cargo` | 15,648 | 2026-10-30 | 无 |
| `rust-lang/rust-clippy` | 13,601 | 2026-10-30 | 无 |
| `serde-rs/serde` | 10,920 | 2026-10-16 | 无 |
| `dtolnay/anyhow` | 6,690 | 2026-10-25 | 无 |

## TypeScript（25）

> vitest/jest 成熟，但 monorepo 与 pnpm workspace 较多，装依赖偏慢

| 仓库 | stars | 最近提交 | 门禁 |
|---|---|---|---|
| `angular/angular` | 101,084 | 2026-10-30 | 无 |
| `puppeteer/puppeteer` | 95,769 | 2026-10-30 | 无 |
| `vitejs/vite` | 83,626 | 2026-10-30 | 无 |
| `grafana/grafana` | 77,583 | 2026-10-30 | 无 |
| `nestjs/nest` | 76,944 | 2026-10-30 | 无 |
| `vuejs/core` | 54,798 | 2026-10-30 | 无 |
| `TanStack/query` | 50,499 | 2026-10-30 | 无 |
| `prisma/orm` | 47,666 | 2026-10-30 | 无 |
| `jestjs/jest` | 45,466 | 2026-10-26 | 无 |
| `colinhacks/zod` | 44,230 | 2026-10-29 | 无 |
| `babel/babel` | 44,055 | 2026-10-30 | 无 |
| `trpc/trpc` | 40,746 | 2026-10-28 | 无 |
| `typeorm/typeorm` | 36,663 | 2026-10-29 | 无 |
| `drizzle-team/drizzle-orm` | 36,180 | 2026-10-30 | 无 |
| `remix-run/remix` | 33,434 | 2026-10-29 | 无 |
| `honojs/hono` | 32,751 | 2026-10-30 | 无 |
| `sequelize/sequelize` | 30,347 | 2026-10-28 | 无 |
| `TanStack/table` | 28,534 | 2026-10-21 | 无 |
| `vitest-dev/vitest` | 17,311 | 2026-10-30 | 无 |
| `vercel/vercel` | 16,472 | 2026-10-30 | 无 |
| `TanStack/router` | 15,261 | 2026-10-30 | 无 |
| `kysely-org/kysely` | 14,316 | 2026-10-30 | 无 |
| `Portkey-AI/gateway` | 13,291 | 2026-05-25 | 无 |
| `mikro-orm/mikro-orm` | 9,266 | 2026-10-30 | 无 |
| `yarnpkg/berry` | 8,121 | 2026-10-27 | 无 |

## JavaScript（12）

> 同 TS，历史包袱更重（babel/webpack 这类需要长构建）

| 仓库 | stars | 最近提交 | 门禁 |
|---|---|---|---|
| `react/react` | 251,756 | 2026-10-30 | 无 |
| `vercel/next.js` | 143,204 | 2026-10-30 | 无 |
| `nodejs/node` | 123,097 | 2026-10-30 | 无 |
| `sveltejs/svelte` | 88,492 | 2026-10-30 | 无 |
| `expressjs/express` | 69,578 | 2026-10-27 | 无 |
| `webpack/webpack` | 65,969 | 2026-10-30 | 无 |
| `prettier/prettier` | 52,382 | 2026-10-30 | 无 |
| `fastify/fastify` | 37,325 | 2026-10-29 | 无 |
| `eslint/eslint` | 27,602 | 2026-10-30 | 无 |
| `rollup/rollup` | 26,305 | 2026-10-30 | 无 |
| `mochajs/mocha` | 22,916 | 2026-10-30 | 无 |
| `npm/cli` | 10,231 | 2026-10-30 | 无 |

## C（4）

> 构建依赖系统库，验证成本高，适合只读分析不适合自动改

| 仓库 | stars | 最近提交 | 门禁 |
|---|---|---|---|
| `redis/redis` | 77,096 | 2026-10-30 | 无 |
| `nginx/nginx` | 32,011 | 2026-10-29 | 无 |
| `valkey-io/valkey` | 27,563 | 2026-10-30 | 无 |
| `pgvector/pgvector` | 23,532 | 2026-10-28 | 无 |

## Java（3）

> gradle/maven 构建重，测试启动慢

| 仓库 | stars | 最近提交 | 门禁 |
|---|---|---|---|
| `elastic/elasticsearch` | 78,415 | 2026-10-30 | 无 |
| `SeleniumHQ/selenium` | 34,572 | 2026-10-30 | 无 |
| `opensearch-project/OpenSearch` | 13,934 | 2026-10-30 | 无 |

## C++（2）

> 同 C，且编译更慢

| 仓库 | stars | 最近提交 | 门禁 |
|---|---|---|---|
| `envoyproxy/envoy` | 29,161 | 2026-10-30 | 无 |
| `apache/arrow` | 17,260 | 2026-10-30 | 无 |

## Jupyter Notebook（1）

> 主要是 notebook，代码测试覆盖弱

| 仓库 | stars | 最近提交 | 门禁 |
|---|---|---|---|
| `guidance-ai/guidance` | 21,810 | 2026-05-21 | 无 |

## C#（1）

> dotnet 单一工具链，可行但生态里中文文档少

| 仓库 | stars | 最近提交 | 门禁 |
|---|---|---|---|
| `microsoft/semantic-kernel` | 28,721 | 2026-10-30 | 无 |
