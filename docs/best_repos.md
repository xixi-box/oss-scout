# 精选贡献仓库名单（38 个）

## 筛选依据

不是按 star 数挑，而是按**能不能真的把 PR 合进去**挑。三个硬指标：

| 指标 | 含义 | 门槛 |
|---|---|---|
| 外部贡献者合并率 | 最近 60 个已关闭 PR 里，非成员提交且被合并的比例 | ≥55% |
| 语言生态 | 自动化验证成本（pytest / go test 最省事） | Python 或 Go |
| PR 门禁 | 是否会关闭未受邀 PR | 无 |

**外部贡献者** = `author_association` 属于 `NONE` / `CONTRIBUTOR` /
`FIRST_TIME_CONTRIBUTOR`。注意这个字段是动态的：一个人第一次被合并后会从
`FIRST_TIME_CONTRIBUTOR` 升级成 `CONTRIBUTOR`，所以只看前者永远是 0。

合并率来自 2026-10-30 的实测快照，会随时间变化。

---

## Python（19）

| 仓库 | 外部合并率 | stars | 简介 |
|---|---|---|---|
| **pypa/virtualenv** | 93% | 5,060 | Virtual Python Environment builder |
| **pypa/pip-tools** | 93% | 8,004 | A set of tools to keep your pinned Python dependencies fresh. |
| **tox-dev/tox** | 90% | 3,979 | Command line driven CI frontend and development task automation tool. |
| **unionai-oss/pandera** | 90% | 4,503 | A light-weight, flexible, and expressive statistical data testing library |
| **aio-libs/aiohttp** | 88% | 16,596 | Asynchronous HTTP client/server framework for asyncio and Python |
| **psf/black** | 88% | 42,073 | The uncompromising Python code formatter |
| **PrefectHQ/prefect** | 82% | 24,101 | Prefect is a workflow orchestration framework for building resilient data pipelines in Python. |
| **microsoft/playwright-python** | 78% | 15,077 | Python version of the Playwright testing and automation library. |
| **apache/airflow** | 76% | 47,320 | Apache Airflow - A platform to programmatically author, schedule, and monitor workflows |
| **openai/openai-python** | 73% | 31,982 | The official Python library for the OpenAI API |
| **fivetran/great_expectations** | 71% | 11,910 | Always know what to expect from your data. |
| **python-poetry/poetry** | 70% | 34,323 | Python packaging and dependency management made easy |
| **scipy/scipy** | 67% | 15,166 | SciPy library main repository |
| **numpy/numpy** | 65% | 33,197 | The fundamental package for scientific computing with Python. |
| **googleapis/python-genai** | 65% | 4,049 | Google Gen AI Python SDK provides an interface for developers to integrate Google's generative models into their Python applications. |
| **deepset-ai/haystack** | 62% | 26,881 | Open-source AI orchestration framework for building context-engineered, production-ready LLM applications. Design modular pipelines and agent workflows with explicit control over retrieval, routing, memory, and generation. Built for scalable agents, RAG, multimodal applications, semantic search, and conversational systems. |
| **pandas-dev/pandas** | 61% | 50,256 | Flexible and powerful data analysis / manipulation library for Python, providing labeled data structures similar to R data.frame objects, statistical functions, and much more |
| **BerriAI/litellm** | 60% | 62,366 | The fastest, litest AI Gateway. Rust core with Python SDK. Call 100+ LLM APIs in OpenAI (or native) format with cost tracking, guardrails, load balancing, and logging [Bedrock, Azure, OpenAI, Anthropic, OpenAI, VertexAI, vLLM, Nvidia NIM] |
| **ansible/ansible-lint** | 59% | 3,928 | ansible-lint checks playbooks for practices and behavior that could potentially be improved and can fix some of the most common ones for you |

## Go（19）

| 仓库 | 外部合并率 | stars | 简介 |
|---|---|---|---|
| **rancher/rancher** | 97% | 26,023 | Complete container management platform |
| **argoproj/argo-workflows** | 96% | 17,069 | Workflow Engine for Kubernetes |
| **redis/go-redis** | 96% | 22,283 | Redis Go client |
| **crossplane/crossplane** | 91% | 12,208 | The Cloud Native Control Plane |
| **gofiber/fiber** | 89% | 40,233 | ⚡️ Express inspired web framework written in Go |
| **hashicorp/consul** | 84% | 30,096 | Consul is a distributed, highly available, and data center aware solution to connect and configure applications across dynamic, distributed infrastructure. |
| **moby/moby** | 83% | 72,222 | The Moby Project - a collaborative project for the container ecosystem to assemble container-based systems |
| **linkerd/linkerd2** | 83% | 11,521 | Ultralight, security-first service mesh for Kubernetes. Main repo for Linkerd 2.x. |
| **urfave/cli** | 81% | 24,306 | A declarative, simple, fast, and fun package for building command line tools in Go |
| **k3s-io/k3s** | 79% | 34,283 | Lightweight Kubernetes |
| **hashicorp/terraform** | 76% | 50,089 | Terraform enables you to safely and predictably create, change, and improve infrastructure. It is a source-available tool that codifies APIs into declarative configuration files that can be shared amongst team members, treated as code, edited, reviewed, and versioned. |
| **argoproj/argo-cd** | 76% | 24,503 | Declarative Continuous Deployment for Kubernetes |
| **pulumi/pulumi** | 74% | 25,833 | Pulumi - Infrastructure as Code in any programming language 🚀 |
| **fluxcd/flux2** | 73% | 8,485 | Open and extensible continuous delivery solution for Kubernetes. Powered by GitOps Toolkit. |
| **prometheus/prometheus** | 72% | 66,856 | The Prometheus monitoring system and time series database. |
| **docker/compose** | 71% | 38,527 | Define and run multi-container applications with Docker |
| **traefik/traefik** | 70% | 65,416 | The Cloud Native Application Proxy |
| **jackc/pgx** | 70% | 14,368 | PostgreSQL driver and toolkit for Go |
| **etcd-io/etcd** | 65% | 52,434 | Distributed reliable key-value store for the most critical data of a distributed system |

---

## 逐仓库说明

每个仓库为什么适合自动化贡献，以及要注意什么。

### rancher/rancher

Kubernetes 管理平台。外部 PR 几乎全收（59/60），CI 完备。规模大但模块边界清晰。

- 外部合并率 **97%** ｜ Go ｜ 26,023 stars

### argoproj/argo-workflows

K8s 工作流引擎。外部合并率 96%，issue 描述通常带完整复现。

- 外部合并率 **96%** ｜ Go ｜ 17,069 stars

### redis/go-redis

Redis 官方 Go 客户端。代码量适中，测试是标准 `go test`，是 Go 侧最容易上手的之一。

- 外部合并率 **96%** ｜ Go ｜ 22,283 stars

### pypa/virtualenv

Python 虚拟环境标准实现。纯 Python，pytest 全覆盖，改动通常 1-3 个文件。

- 外部合并率 **93%** ｜ Python ｜ 5,060 stars

### pypa/pip-tools

依赖锁定工具。纯 Python，测试快，外部贡献门槛低。

- 外部合并率 **93%** ｜ Python ｜ 8,004 stars

### crossplane/crossplane

K8s 控制平面框架。Go，需注意 CRD 变更要同步测试。

- 外部合并率 **91%** ｜ Go ｜ 12,208 stars

### tox-dev/tox

Python 测试环境管理。纯 Python，测试跑得快，适合小修复。

- 外部合并率 **90%** ｜ Python ｜ 3,979 stars

### unionai-oss/pandera

数据校验库。纯 Python，pytest，改动范围小。

- 外部合并率 **90%** ｜ Python ｜ 4,503 stars

### gofiber/fiber

Go Web 框架。文档和测试都规范，外部合并率 89%。

- 外部合并率 **89%** ｜ Go ｜ 40,233 stars

### aio-libs/aiohttp

异步 HTTP 客户端/服务端。纯 Python，测试体系成熟，但异步代码要留意事件循环问题。

- 外部合并率 **88%** ｜ Python ｜ 16,596 stars

### psf/black

Python 代码格式化器。测试以 fixture 快照为主，改格式化逻辑要同步更新快照。

- 外部合并率 **88%** ｜ Python ｜ 42,073 stars

### hashicorp/consul

服务发现与配置。Go，测试需要本地起 agent，稍重。

- 外部合并率 **84%** ｜ Go ｜ 30,096 stars

### moby/moby

Docker 引擎。Go，构建慢（首次 10 分钟+），建议只挑明确的小 bug。

- 外部合并率 **83%** ｜ Go ｜ 72,222 stars

### linkerd/linkerd2

服务网格。Go，需 K8s 环境跑集成测试，优先单元测试覆盖的部分。

- 外部合并率 **83%** ｜ Go ｜ 11,521 stars

### PrefectHQ/prefect

数据编排。Python，pytest，但依赖较多，装环境需要几分钟。

- 外部合并率 **82%** ｜ Python ｜ 24,101 stars

### urfave/cli

Go CLI 框架。代码量小，纯 `go test`，是最容易上手的 Go 仓库之一。

- 外部合并率 **81%** ｜ Go ｜ 24,306 stars

### k3s-io/k3s

轻量 K8s。Go，规模大，优挑选 kubectl/CLI 层的 bug。

- 外部合并率 **79%** ｜ Go ｜ 34,283 stars

### microsoft/playwright-python

浏览器自动化 Python 绑定。外部合并率高，但测试常需要装浏览器，耗时。

- 外部合并率 **78%** ｜ Python ｜ 15,077 stars

### apache/airflow

工作流编排。Python，规模巨大，务必挑单 provider 的小问题。

- 外部合并率 **76%** ｜ Python ｜ 47,320 stars

### hashicorp/terraform

基础设施即代码。Go，测试多为单元测试，验收测试需要云凭据（跳过）。

- 外部合并率 **76%** ｜ Go ｜ 50,089 stars

### argoproj/argo-cd

GitOps 持续部署。Go，有 auto-assign bot 但不关 PR。

- 外部合并率 **76%** ｜ Go ｜ 24,503 stars

### pulumi/pulumi

IaC 平台。Go，已验证可全自动修复（#25467 就是我们提的）。社区 PR 只留言提示，不关闭。

- 外部合并率 **74%** ｜ Go ｜ 25,833 stars

### openai/openai-python

OpenAI 官方 Python SDK。外部合并率 73%，测试是标准 pytest，但大量测试要 mock HTTP。

- 外部合并率 **73%** ｜ Python ｜ 31,982 stars

### fluxcd/flux2

GitOps 工具。Go，代码量适中。

- 外部合并率 **73%** ｜ Go ｜ 8,485 stars

### prometheus/prometheus

监控系统。Go，规模大，优先挑 promql/配置解析类问题。

- 外部合并率 **72%** ｜ Go ｜ 66,856 stars

### fivetran/great_expectations

数据质量框架。Python，有 `/assign-me` 认领机制，最好先认领再动手。

- 外部合并率 **71%** ｜ Python ｜ 11,910 stars

### docker/compose

Docker 编排 CLI。Go，`go test` 为主，规模适中。

- 外部合并率 **71%** ｜ Go ｜ 38,527 stars

### python-poetry/poetry

Python 依赖管理。纯 Python，测试完备，但 fixture 重。

- 外部合并率 **70%** ｜ Python ｜ 34,323 stars

### traefik/traefik

反向代理。Go，规模大但模块清晰。

- 外部合并率 **70%** ｜ Go ｜ 65,416 stars

### jackc/pgx

PostgreSQL Go 驱动。Go，测试需要本地 PG（CI 里有 docker 启动）。

- 外部合并率 **70%** ｜ Go ｜ 14,368 stars

### scipy/scipy

科学计算。Python，编译型依赖多，装环境慢（首次几分钟），优挑选纯 Python 模块的问题。

- 外部合并率 **67%** ｜ Python ｜ 15,166 stars

### numpy/numpy

数值计算基础库。同 scipy，编译安装慢，优先纯 Python 层。

- 外部合并率 **65%** ｜ Python ｜ 33,197 stars

### googleapis/python-genai

Google GenAI Python SDK。外部合并率 65%，SDK 类仓库测试以 mock 为主，容易写回归测试。

- 外部合并率 **65%** ｜ Python ｜ 4,049 stars

### etcd-io/etcd

分布式键值存储。Go，规模大，优先挑 client/CLI 层。

- 外部合并率 **65%** ｜ Go ｜ 52,434 stars

### deepset-ai/haystack

RAG 框架。Python，PR 标题要求 conventional commits。

- 外部合并率 **62%** ｜ Python ｜ 26,881 stars

### pandas-dev/pandas

数据分析。Python，规模大，务必挑单函数的小 bug，且要跑特定测试子集。

- 外部合并率 **61%** ｜ Python ｜ 50,256 stars

### BerriAI/litellm

LLM 网关。Python，限制档：PR 必须带测试，按其 CONTRIBUTING checklist 逐项过。

- 外部合并率 **60%** ｜ Python ｜ 62,366 stars

### ansible/ansible-lint

Ansible 代码检查。纯 Python，pytest，改动局部。

- 外部合并率 **59%** ｜ Python ｜ 3,928 stars
