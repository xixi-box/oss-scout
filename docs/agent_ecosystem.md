# Agent Harness 及生态调研（2026-10-30）

数据来源：GitHub API 实测（star / 最近提交）+ Exa 搜索（社区讨论）。
**star 数只作规模参考**，选型要看最近提交时间和是否还在活跃维护。

---

## 一、Agent Harness（运行时骨架）

Harness 是把 LLM 变成能干活的东西的那层壳：驱动模型与工具调用、管会话状态、
施加审批策略、维持多步任务推进。

### 闭源标杆（心智模型参考）

| 项目 | stars | 说明 |
|---|---|---|
| `anthropics/claude-code` | 153.9k | TS。最成熟的商业 coding harness。它定义了"harness 应该有啥"——权限模型、todo、subagent、hook、skill |

### 开源 harness

| 项目 | stars | 语言 | 最近提交 | 说明 |
|---|---|---|---|---|
| `All-Hands-AI/OpenHands` | 92.3k | TS | 10-30 | AI 驱动开发。沙箱内跑，有完整的 event stream |
| `bytedance/deer-flow` | 84.7k | Python | 10-30 | 长时程 SuperAgent harness |
| `cline/cline` | 71.1k | TS | 10-30 | 自治 coding agent，可作 SDK / IDE 扩展 / CLI 三态 |
| `aaif-goose/goose` | 56.0k | Rust | 10-30 | 可扩展 agent，本地优先（原 block/goose） |
| `langchain-ai/deepagents` | 30.8k | Python | 10-30 | 官方"电池内置"harness |
| `openai/openai-agents-python` | 30.4k | Python | 10-27 | 轻量多 agent 框架 |
| `pydantic/pydantic-ai` | 21.0k | Python | 10-30 | Python 类型安全的 agent 框架 |
| `openai/swarm` | 22.1k | Python | **04-15** | 教学用途，**已停更**，不要生产用 |

### 关键判断

**harness 与 framework 的区别**：framework 提供抽象让你拼装；harness 是"已经拼好的、
能直接跑任务的"。做产品选 harness，做定制选 framework。

**Rust 系在变多**（goose、deno/bun 生态）：启动快、单二进制、内存可控。代价是插件生态弱。

---

## 二、Agent Memory（长期记忆）

### 三强对比

| 项目 | stars | 语言 | 最近提交 | 定位 |
|---|---|---|---|---|
| `mem0ai/mem0` | 68.1k | Python | 10-29 | **drop-in 记忆层**，最易集成。向量+图混合 |
| `letta-ai/letta` | 25.3k | — | 10-18 | 前身 MemGPT。"有状态 agent 平台"，自带 harness + memory |
| `getzep/zep` | 5.0k | Python | 10-20 | 时序知识图谱路线（Graphiti）。适合"事实随时间变化"的场景 |

### 选型建议

- **只想加记忆、不想换框架** → `mem0`。API 简单，能挂在任何 agent 后面
- **要 agent 自己管理记忆、能自我编辑** → `letta`。但它是整套平台，绑定较深
- **记忆里的事实会随时间失效**（如"用户现在的职位"）→ `zep`。纯向量检索处理不了时序

### 其他思路

| 项目 | stars | 说明 |
|---|---|---|
| `tigerless-labs/agent-memory` | 4.4k | 纯 Markdown 存记忆，无数据库依赖。适合小规模 |
| `volcengine/OpenViking` | 41.2k | 自称"自演化上下文数据库"，较新 |
| `vectorize-io/hindsight` | 56.4k | 主打"会学习的记忆" |

**警惕**：memory 这个赛道名词通胀严重，很多项目只是"向量库 + 提示词模板"。
判断标准是**它是否解决了"检索什么"而不只是"怎么存"**。

---

## 三、RAG 生态

### 框架层

| 项目 | stars | 最近提交 | 说明 |
|---|---|---|---|
| `langchain-ai/langchain` | 148.7k | 10-29 | 事实标准。抽象多、学习曲线陡，但生态最全 |
| `run-llama/llama_index` | 52.7k | 10-28 | 文档处理见长，RAG 是一等公民 |
| `HKUDS/RAG-Anything` | 24.3k | 10-27 | 一站式 RAG |
| `microsoft/markitdown` | 193.0k | 10-30 | **不是 RAG**，但预处理必备：把 pdf/docx/pptx 转 Markdown |

### 向量数据库

| 项目 | stars | 语言 | 最近提交 | 适用 |
|---|---|---|---|---|
| `milvus-io/milvus` | 46.5k | Go | 10-30 | 亿级，需要独立部署 |
| `qdrant/qdrant` | 35.3k | Rust | 10-29 | **默认推荐**：单机够用，Rust 性能好，过滤强 |
| `chroma-core/chroma` | 29.6k | Rust | 10-28 | 嵌入式，开发期最省事 |
| `pgvector/pgvector` | 23.5k | C | 10-28 | **已有 Postgres 就别上专用库**，省一套运维 |
| `weaviate/weaviate` | 16.9k | Go | 10-30 | 自带混合检索（向量+BM25） |

### 检索质量的关键环节（常被忽略）

1. **切分**：语义切分 > 固定长度。这是 RAG 效果差异最大的地方
2. **混合检索**：纯向量检索对专有名词、代码符号很差，要配 BM25
3. **重排（rerank）**：召回 50 条 → rerank 到 5 条，效果提升明显
4. **上下文压缩**：`headroomlabs-ai/headroom`（78.3k）就是这个思路

---

## 四、MCP（Model Context Protocol）生态

### 协议与基础设施

| 项目 | stars | 语言 | 说明 |
|---|---|---|---|
| `modelcontextprotocol/servers` | 91.7k | TS | **官方 server 集合**，入门必读 |
| `PrefectHQ/fastmcp` | 28.2k | Python | **写 MCP server 的首选**，比官方 SDK 好用 |
| `punkpeye/awesome-mcp-servers` | 97.1k | — | 索引，找现成 server 用这个 |
| `upstash/context7` | 64.0k | TS | 给 agent 提供"最新的库文档"，解决训练数据过期 |

### 值得注意的 server

| 项目 | stars | 说明 |
|---|---|---|
| `microsoft/playwright-mcp` | 38.9k | 浏览器自动化。比直接给 agent 塞 puppeteer 稳 |
| `github/github-mcp-server` | 34.1k | 官方，Go 写的 |
| `activepieces/activepieces` | 25.3k | ~400 个 MCP 的聚合平台 |

### 生态现状判断

MCP 已经是事实标准（OpenAI、Anthropic、Google 都支持）。但**质量问题严重**：
awesome 列表里大量 server 是"包一层 REST API"，没有考虑 agent 的上下文预算。

判断一个 MCP server 好不好：**看它返回的内容是否对 LLM 友好**（结构化、无冗余、
有明确的错误语义），而不是看它能调多少接口。

---

## 五、Agent 评估与可观测

| 项目 | stars | 语言 | 最近提交 | 说明 |
|---|---|---|---|---|
| `Arize-ai/phoenix` | 12.1k | Python | 10-30 | **可观测性 + 评估一体**。trace 做得最好 |
| `confident-ai/deepeval` | 19.2k | Python | 10-29 | pytest 风格的单测框架，适合接 CI |
| `explodinggradients/ragas` | 16.2k | Python | **02-24** | RAG 专用指标。**停更半年，慎用** |
| `awslabs/agent-evaluation` | 377 | Python | — | AWS 官方，小众 |

### 评估的三种粒度

1. **最终输出对不对** — 最简单，但看不出过程错误
2. **轨迹（trajectory）对不对** — agent 走了哪些步、调了哪些工具。**这才是 agent 评估的核心**
3. **单步决策对不对** — 细粒度，成本高

现在多数工具只做第 1 种。`phoenix` 的 trace 能支撑第 2 种。

---

## 六、工具调用与沙箱执行

| 项目 | stars | 语言 | 说明 |
|---|---|---|---|
| `browser-use/browser-use` | 119.6k | Python | 浏览器 agent，事实标准 |
| `e2b-dev/e2b` | — | — | 云端沙箱，按秒计费 |
| Modal Sandbox | — | — | 临时容器，适合跑不可信代码 |

**教训（来自 oss-scout 实践）**：交互式 CLI（如 opencode）在无 tty 的瘦容器里
会卡在 bootstrap（权限询问、插件安装）。要么给 `--auto` 类参数，要么换托管 runner。
沙箱适合**短任务**（扫描、单次测试），长任务要选有完整用户态的环境。

---

## 七、选型速查

| 需求 | 推荐 |
|---|---|
| 直接能用的 coding agent | `claude-code`（闭源）/ `OpenHands` / `cline` |
| 自己搭 agent，Python | `openai-agents-python` 或 `pydantic-ai` |
| 要"电池内置" | `deepagents` |
| 加长期记忆 | `mem0` |
| 记忆事实会过期 | `zep` |
| 向量库，单机 | `qdrant` |
| 已有 Postgres | `pgvector` |
| 写 MCP server | `fastmcp` |
| agent 可观测 | `phoenix` |
| agent 单测 | `deepeval` |

---

## 八、几条判断

1. **框架战争已经结束**：LangChain / LlamaIndex 是存量标准，新项目更多直接用
   provider 官方 SDK（openai-agents、pydantic-ai）+ 自己拼。

2. **harness 正在商品化**：Claude Code 定义了形态，开源在快速跟进（deepagents、
   deer-flow、cline）。差异化不在 harness 本身，在你给它什么工具和约束。

3. **Memory 是当前最混乱的赛道**：名词通胀、方案互不兼容。建议先用最简单的
   （Markdown 文件 / pgvector），而不是一上来接专用平台。

4. **MCP 生态大但质量参差**：协议是好的，但多数 server 没为 LLM 上下文优化。
   用之前先看它返回什么。

5. **评估是最被低估的环节**：多数团队只测最终输出，不测轨迹。而 agent 的失败
   往往在中间步骤。
