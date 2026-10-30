# 最终可提交仓库名单

生成时间 2026-10-30。测量方法：取每个仓库最近 60 个已关闭 PR，统计
**非成员提交且被合并的比例**（外部贡献者 = `author_association` 属于
`NONE` / `CONTRIBUTOR` / `FIRST_TIME_CONTRIBUTOR`）。

筛选条件：合并率 ≥50%、语言可自动验证（Python/Go/TS/JS/Rust）、
无会关闭未受邀 PR 的 bot、非停更、样本量足够。

共 **40** 个。

---

## 按类别

### 工程工具

| 仓库 | 合并率 | stars | 说明 |
|---|---|---|---|
| `pypa/virtualenv` | 93% | 5,060 | Python 虚拟环境标准实现 |
| `pypa/pip-tools` | 93% | 8,004 | 依赖锁定 |
| `tox-dev/tox` | 90% | 3,979 | 测试环境矩阵 |
| `biomejs/biome` | 90% | 26,024 | Rust 写的 JS/TS 工具链 |
| `psf/black` | 88% | 42,074 | 代码格式化 |
| `astral-sh/uv` | 76% | 91,362 | 极快的 Python 包管理器 |
| `astral-sh/ruff` | 64% | 50,337 | Rust 写的 Python linter |

### Agent Memory

| 仓库 | 合并率 | stars | 说明 |
|---|---|---|---|
| `vectorize-io/hindsight` | 100% | 56,409 | 主打自学习记忆 |
| `tigerless-labs/agent-memory` | 80% | 4,377 | 纯 Markdown 存记忆，无数据库依赖 |
| `volcengine/OpenViking` | 65% | 41,191 | 自演化上下文数据库 |
| `oceanbase/powercontext` | 57% | 1,429 | 长期记忆 + 跨 agent 交接 |
| `ruvnet/RuVector` | 53% | 4,648 | 高性能实时决策向量层 |

### 向量库

| 仓库 | 合并率 | stars | 说明 |
|---|---|---|---|
| `lancedb/lancedb` | 87% | 11,816 | 嵌入式列式向量库，无服务端 |
| `milvus-io/milvus` | 76% | 46,536 | 云原生，亿级规模 |
| `weaviate/weaviate` | 74% | 16,930 | 自带混合检索（向量+BM25） |
| `qdrant/qdrant` | 60% | 35,334 | Rust，单机首选，过滤强 |

### MCP

| 仓库 | 合并率 | stars | 说明 |
|---|---|---|---|
| `upstash/context7` | 63% | 63,953 | 给 agent 提供最新库文档 |
| `PrefectHQ/fastmcp` | 61% | 28,245 | 写 MCP server 的首选库 |
| `modelcontextprotocol/registry` | 57% | 7,423 | 官方 server 注册表 |

### Agent 框架

| 仓库 | 合并率 | stars | 说明 |
|---|---|---|---|
| `openai/openai-agents-python` | 91% | 30,394 | OpenAI 官方轻量多 agent 框架，Python 类型友好 |
| `VoltAgent/voltagent` | 53% | 10,901 | TypeScript 版 agent 工程平台 |

### 评估

| 仓库 | 合并率 | stars | 说明 |
|---|---|---|---|
| `promptfoo/promptfoo` | 86% | 26,629 | prompt 测试与红队 |
| `confident-ai/deepeval` | 51% | 19,198 | pytest 风格 agent 单测 |

### RAG

| 仓库 | 合并率 | stars | 说明 |
|---|---|---|---|
| `HKUDS/RAG-Anything` | 86% | 24,313 | 一站式 RAG 框架 |
| `OSU-NLP-Group/HippoRAG` | 52% | 4,083 | NeurIPS'24，类海马体检索思路 |

### 实时协作

| 仓库 | 合并率 | stars | 说明 |
|---|---|---|---|
| `liveblocks/liveblocks` | 82% | 4,764 | 协作基础设施 |
| `yjs/yjs` | 68% | 23,001 | CRDT 事实标准 |

### 浏览器自动化

| 仓库 | 合并率 | stars | 说明 |
|---|---|---|---|
| `microsoft/playwright-python` | 78% | 15,077 | Playwright 的 Python 绑定 |
| `microsoft/playwright` | 62% | 98,870 | 跨浏览器自动化框架 |

### 去中心化

| 仓库 | 合并率 | stars | 说明 |
|---|---|---|---|
| `dxos/dxos` | 100% | 532 | 本地优先的协作运行时 |

### 实时数据

| 仓库 | 合并率 | stars | 说明 |
|---|---|---|---|
| `instantdb/instant` | 93% | 10,565 | 前端直连数据库 |

### 可观测/评估

| 仓库 | 合并率 | stars | 说明 |
|---|---|---|---|
| `Arize-ai/phoenix` | 86% | 12,075 | trace + eval 一体，agent 轨迹可视 |

### 文档

| 仓库 | 合并率 | stars | 说明 |
|---|---|---|---|
| `zuplo/zudoku` | 84% | 613 | OpenAPI 驱动 API 文档 |

### 沙箱

| 仓库 | 合并率 | stars | 说明 |
|---|---|---|---|
| `firecracker-microvm/firecracker` | 77% | 37,721 | AWS 的 microVM，跑不可信代码的事实标准 |

### 后端平台

| 仓库 | 合并率 | stars | 说明 |
|---|---|---|---|
| `supabase/supabase` | 72% | 114,339 | Postgres + 全家桶 |

### 推理服务

| 仓库 | 合并率 | stars | 说明 |
|---|---|---|---|
| `vllm-project/vllm` | 69% | 95,911 | 高吞吐 LLM 推理 |

### 可观测

| 仓库 | 合并率 | stars | 说明 |
|---|---|---|---|
| `langfuse/langfuse` | 62% | 36,755 | LLM 应用追踪，开源自托管 |

### Agent Harness

| 仓库 | 合并率 | stars | 说明 |
|---|---|---|---|
| `bytedance/deer-flow` | 59% | 84,719 | 长时程 SuperAgent，研究向 |

### 画布

| 仓库 | 合并率 | stars | 说明 |
|---|---|---|---|
| `tldraw/tldraw` | 58% | 51,160 | 无限画布 SDK |

### LLM 网关

| 仓库 | 合并率 | stars | 说明 |
|---|---|---|---|
| `BerriAI/litellm` | 57% | 62,374 | 统一 100+ 模型接口 |

---

## 完整排序（按合并率）

| # | 仓库 | 合并率 | 外部样本 | 语言 | stars | 最近提交 |
|---|---|---|---|---|---|---|
| 1 | `vectorize-io/hindsight` | 100% | 15/15 | Python | 56,409 | 2026-10-30 |
| 2 | `dxos/dxos` | 100% | 35/35 | TypeScript | 532 | 2026-10-30 |
| 3 | `instantdb/instant` | 93% | 56/60 | TypeScript | 10,565 | 2026-10-19 |
| 4 | `pypa/virtualenv` | 93% | 56/60 | Python | 5,060 | 2026-10-28 |
| 5 | `pypa/pip-tools` | 93% | 54/58 | Python | 8,004 | 2026-10-26 |
| 6 | `openai/openai-agents-python` | 91% | 32/35 | Python | 30,394 | 2026-10-27 |
| 7 | `tox-dev/tox` | 90% | 19/21 | Python | 3,979 | 2026-10-30 |
| 8 | `biomejs/biome` | 90% | 45/50 | Rust | 26,024 | 2026-10-30 |
| 9 | `psf/black` | 88% | 49/56 | Python | 42,074 | 2026-10-30 |
| 10 | `lancedb/lancedb` | 87% | 47/54 | Rust | 11,816 | 2026-10-30 |
| 11 | `promptfoo/promptfoo` | 86% | 51/59 | TypeScript | 26,629 | 2026-10-30 |
| 12 | `Arize-ai/phoenix` | 86% | 31/36 | Python | 12,075 | 2026-10-30 |
| 13 | `HKUDS/RAG-Anything` | 86% | 42/49 | Python | 24,313 | 2026-10-28 |
| 14 | `zuplo/zudoku` | 84% | 48/57 | TypeScript | 613 | 2026-10-29 |
| 15 | `liveblocks/liveblocks` | 82% | 49/60 | TypeScript | 4,764 | 2026-10-30 |
| 16 | `tigerless-labs/agent-memory` | 80% | 37/46 | Python | 4,377 | 2026-10-30 |
| 17 | `microsoft/playwright-python` | 78% | 18/23 | Python | 15,077 | 2026-10-29 |
| 18 | `firecracker-microvm/firecracker` | 77% | 46/60 | Rust | 37,721 | 2026-10-30 |
| 19 | `astral-sh/uv` | 76% | 29/38 | Rust | 91,362 | 2026-10-30 |
| 20 | `milvus-io/milvus` | 76% | 29/38 | Go | 46,536 | 2026-10-30 |
| 21 | `weaviate/weaviate` | 74% | 35/47 | Go | 16,930 | 2026-10-30 |
| 22 | `supabase/supabase` | 72% | 37/51 | TypeScript | 114,339 | 2026-10-30 |
| 23 | `vllm-project/vllm` | 69% | 29/42 | Python | 95,911 | 2026-10-30 |
| 24 | `yjs/yjs` | 68% | 36/53 | JavaScript | 23,001 | 2026-10-23 |
| 25 | `volcengine/OpenViking` | 65% | 15/23 | Python | 41,191 | 2026-10-30 |
| 26 | `astral-sh/ruff` | 64% | 21/33 | Rust | 50,337 | 2026-10-30 |
| 27 | `upstash/context7` | 63% | 26/41 | TypeScript | 63,953 | 2026-10-30 |
| 28 | `microsoft/playwright` | 62% | 20/32 | TypeScript | 98,870 | 2026-10-30 |
| 29 | `langfuse/langfuse` | 62% | 33/53 | TypeScript | 36,755 | 2026-10-30 |
| 30 | `PrefectHQ/fastmcp` | 61% | 11/18 | Python | 28,245 | 2026-10-30 |
| 31 | `qdrant/qdrant` | 60% | 18/30 | Rust | 35,334 | 2026-10-29 |
| 32 | `bytedance/deer-flow` | 59% | 32/54 | Python | 84,719 | 2026-10-30 |
| 33 | `tldraw/tldraw` | 58% | 21/36 | TypeScript | 51,160 | 2026-10-30 |
| 34 | `modelcontextprotocol/registry` | 57% | 16/28 | Go | 7,423 | 2026-10-30 |
| 35 | `oceanbase/powercontext` | 57% | 29/51 | Python | 1,429 | 2026-10-30 |
| 36 | `BerriAI/litellm` | 57% | 34/60 | Python | 62,374 | 2026-10-30 |
| 37 | `VoltAgent/voltagent` | 53% | 17/32 | TypeScript | 10,901 | 2026-10-15 |
| 38 | `ruvnet/RuVector` | 53% | 17/32 | Rust | 4,648 | 2026-10-30 |
| 39 | `OSU-NLP-Group/HippoRAG` | 52% | 15/29 | Python | 4,083 | 2026-10-06 |
| 40 | `confident-ai/deepeval` | 51% | 26/51 | Python | 19,198 | 2026-10-29 |

---

## 剔除记录

测量过但不适合提交的，附原因。

### 会关闭未受邀 PR（硬门禁）

| 仓库 | 原因 |
|---|---|
| `mem0ai/mem0` | `pr-gate.yml` 明确关闭未事先同意承接的 PR |
| `pydantic/pydantic-ai` | `pr-gates.yml` 关闭未指派 PR |
| `langchain-ai/deepagents` | `require_issue_link` 会关闭未指派 PR |
| `langchain-ai/langchain` | `require-issue-link` bot |
| `langchain-ai/langgraph` | `require_issue_link.yml` |
| `pydantic/pydantic` | `enforce-issue-assignment.yml` |
| `sqlalchemy/sqlalchemy` | `sqla-tester` 需要 issue 带 invite label |

### 停更 / 样本不足 / 超大单体

| 仓库 | 原因 |
|---|---|
| `llm-tools/embedJs` | 2026-06 后停更 |
| `awslabs/agent-evaluation` | 2025-12 后停更 |
| `Significant-Gravitas/AutoGPT` | 超大单体仓库，issue 多在深水区 |

### 合并率过低（<50%）

测过但外部 PR 大多合不进去，列几个代表：

- `cline/cline` — 49%（29/59）
- `All-Hands-AI/OpenHands` — 48%（13/27）
- `chroma-core/chroma` — 47%（28/60）
- `huggingface/text-embeddings-inference` — 46%（21/46）
- `microsoft/playwright-mcp` — 45%（18/40）
- `TransformerOptimus/SuperAGI` — 44%（15/34）
- `getzep/graphiti` — 44%（21/48）
- `alibaba/zvec` — 44%（7/16）
- `vitejs/vite` — 42%（18/43）
- `openai/swarm` — 42%（10/24）
- `open-webui/mcpo` — 41%（16/39）
- `github/github-mcp-server` — 41%（20/49）
- `nmslib/hnswlib` — 40%（19/48）
- `modelcontextprotocol/typescript-sdk` — 39%（22/56）

---

## 说明

**合并率是快照，会变。** 建议每隔一两个月重跑一次：

```bash
python scripts/audit_contrib_friendliness.py --pool <名单> --out <输出>
python scripts/merge_pool_results.py
```

**合并率高不等于容易修。** 它只说明「这个仓库愿意收外人的 PR」，
具体某个 issue 好不好修，还要看 issue 本身的描述质量。

**星数只作规模参考。** 一个 5k star 但合并率 90% 的仓库，比 90k star
但合并率 30% 的仓库更容易出成果。
