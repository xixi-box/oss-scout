# oss-scout v2：Issue Hunter + Bug Hunter 自动化贡献系统

版本：实施草案 v1.0  
日期：2026-10-24  
面向：本地 Coding Agent（Windows Git Bash / Python）、VPS（Linux 2C2G）、Modal Sandbox、GitHub Actions  
核心原则：**复用成熟组件、只补薄适配层；只读发现可以并行，外部 GitHub 写入必须集中授权；免费优先，无法确认预算即停止。**

> 重要：这是一份实施说明，不是已验证现有源码的审计结论。本地 Agent **必须先阅读** `G:\project\oss-scout\AGENTS.md`（Git Bash：`/g/project/oss-scout/AGENTS.md`）、已有脚本及 state 文件。若本计划与 Playbook 冲突，以 Playbook 为准，记录冲突并禁用相关功能；不得擅自更改原规则。

## 0. 项目目标与明确不做的事

### 目标

1. VPS 7×24 小时低资源在线：Issue 发现、候选更新、规则判断、PR 状态跟踪、任务队列。
2. Bug Hunter 不依赖现成 Issue：轻量元数据与 Diff 初筛留在 VPS；跨第三方仓库的深度扫描与复现交给 Modal。
3. 合规的具体项目修复、项目内测试与 CI 优先用 GitHub Actions；不可用时，若 Modal 预算允许再使用 Modal。
4. 两套计算后端运行**一份业务执行器**（相同任务契约、分析逻辑、回归验证逻辑、结果格式），仅运行环境的适配层不同。
5. AI Coding Agent 使用 OpenCode CLI，免费模型优先；付费 API 默认禁用。Agent 对修复仅提供实施能力，回归测试和仓库规范决定是否合格。
6. 贡献前严格遵守仓库 Playbook / CONTRIBUTING / Issue/PR 模板及仓库维护者沟通流程。
7. 可随时将系统退化回原先的 `discover.py + health_check.py + 3 窗消费`，不丢历史状态。

### 非目标（初期不要实现）

- 不自建 Windmill、OpenHands、CodeQL 或复杂 Web UI；不用 Kubernetes、Redis、Kafka。
- 不在 2C2G VPS 上运行大型项目的测试、编译或全仓深度静态分析。
- 不用 GitHub Actions 免费公共 Runner 批量扫描与该工作流仓库无关的第三方项目，也不为了绕过限制批量 Fork。
- 不在发现阶段自动 Fork 所有关注的仓库。
- 不在 Playbook 未授权时自动评论、认领、Push、创建 PR；不把 AI 推测当作已证实的 Bug。
- 不用 ChatGPT Plus/Codex 浏览器 Cookie 或复制本地登录会话作为云端自动化认证。

## 1. 固定选型和平台职责

| 组件 | 角色 | 部署位置 | 版本与替代策略 |
|---|---|---|---|
| 已有 `oss-scout` / `discover.py` / `health_check.py` | Issue Hunter、兼容历史流程 | VPS / 本地 | 先保持，再增量增强 |
| GitHub API / CLI | Repo/Issue/PR 查询及合规 GitHub 写操作 | VPS 控制器 | 现成工具优先 |
| Python 3.11+、`systemd timer` | 调度、轮询、后台任务 | VPS | 不上 Windmill |
| SQLite WAL（后期可选） | 任务队列、租约、原子配额、幂等键 | VPS | 保留 JSONL 和 JSON 兼容；首阶段可用文件锁过渡 |
| Semgrep（规则精选） | 静态问题线索，不作为真实 Bug 的唯一证据 | Modal Worker | 有测试才算验证 |
| OpenCode CLI | 唯一主要 Coding Agent | Modal / GitHub Actions | 两边同版本 |
| Modal Sandbox | 广域第三方代码深度扫描；CI 不适用时的备用计算 | Modal | $30/月的计算抵扣以官方当前规则为准 |
| GitHub Actions | **具体项目**的修复、构建、测试、Review | 有权限的对应项目 Fork/仓库 | 遵守使用条款；不是通用免费扫描云 |
| 飞书 Webhook | 重要事件与每日报告 | VPS | 存在才启用 |

关键技术事实（实施时再次确认）：公共仓库标准 `ubuntu-latest` Runner 官方显示 4 vCPU、16 GB RAM、14 GB SSD；私有仓库 Runner 按其账户配额与计费规则执行。Github Actions 不保证任何仓库 Fork 都能立即运行工作流；工作流必须存在、启用且具备适当权限。

## 2. 总体数据流

```text
VPS: scheduled polling
  ├─ Issue Hunter: GitHub API / existing discover.py → Issue candidates
  ├─ Light Bug Hunter: repo metadata / changed-file index / selected diffs → suspected candidates
  └─ PR monitor: CI / review / merged / closed → state updates
                             │
                             ▼
               normalized candidate store / dedupe
                             │
                    repository policy gate
                             │
                 job router / idempotent queue
                      ┌──────┴─────────┐
                      ▼                ▼
              Modal Sandbox       GitHub Actions
          (broad exploration)    (project-related work)
                      └──────┬─────────┘
                             ▼
           unified worker code: scan / reproduce / fix / review
                             │
                result.json + patch + test logs
                             │
          trusted controller: check freshness / quota / policy
                             │
                 permission-based comment / fork / PR
```

**不能直接“所有任务 Actions 优先”**：发现性质的广域深度扫描只走 Modal/其他合规计算资源；具体项目的代码修改和 CI 才优先走 Actions。Modal 用完时，广域深度扫描排队至额度重置，VPS 的轻量发现仍继续。

## 3. 最小目录结构（在原仓库目录内新增，严禁删除既有目录）

```text
oss-scout/
  AGENTS.md                         # 原有 Playbook，未经允许不改硬规则
  discover.py                       # 保留
  health_check.py                   # 保留
  queue/candidates.json             # 原兼容视图
  state/worklog.jsonl               # 原审计事实与贡献历史
  state/health.json
  state/runs/...
  configs/
    v2.yaml                         # 调度、后端预算、限流、feature flags
    repositories.yaml                # 人工审核的仓库策略覆盖
    model_policy.yaml                # OpenCode 模型优先级/费用保护
  scout_v2/
    schemas.py                       # Pydantic 或 dataclass 输入输出
    candidate_normalizer.py
    repo_policy.py                   # 只授予操作，不执行模型意图
    budget.py
    scheduler.py
    job_router.py
    git_host.py                      # GitHub 获取/校验
    state_store.py                   # SQLite / JSONL 兼容
    workers/
      core.py                        # 共用逻辑，与云厂商无关
      scan.py
      reproduce.py
      repair.py
      quality_gate.py
      model_fallback.py
    adapters/
      modal_backend.py
      actions_backend.py
      local_backend.py                 # 测试用，不在 VPS 上跑重活
  docker/
    Dockerfile.worker
    entrypoint.sh
  .github/workflows/                    # 仅测试本系统；不要借此扫描无关项目
  tests_v2/
    fixtures/
    test_policy.py
    test_quota_concurrency.py
    test_budget.py
    test_router.py
    test_backends_mock.py
    test_worklog_compat.py
    test_security.py
  docs/
    architecture.md
    operator_runbook.md
    migration.md
```

这只是建议结构。若现有项目已有同职责模块，应复用其结构，避免创建重复的控制器和状态源。

## 4. 数据契约：候选、任务、结果

### 4.1 Candidate（示意，保持兼容旧字段）

```json
{
  "candidate_id": "github:owner/repo:issue:123",
  "lane": "issue",
  "repo": "owner/repo",
  "issue_number": 123,
  "base_sha": null,
  "title": "Reproducible parsing failure",
  "url": "https://github.com/owner/repo/issues/123",
  "grade": "A",
  "repro_status": "unverified",
  "competition_status": "unknown",
  "policy_status": "pending",
  "status": "discovered",
  "first_seen_at": "2026-10-24T00:00:00Z",
  "updated_at": "2026-10-24T00:00:00Z",
  "evidence_paths": []
}
```

主动 Bug 使用 `lane: "bug"`、`candidate_id: github:owner/repo:bug:<sha>:<fingerprint>`、`issue_number: null`。Bug 不一定有 Issue，不要为适配旧结构强行创建 Issue。证据用 commit SHA + 文件/行号 + 预期行为来源 + 复现测试标识，不用 LLM 置信度直接当概率。

### 4.2 Job（统一执行请求）

```json
{
  "schema_version": 1,
  "job_id": "job-uuid",
  "idempotency_key": "verify:owner/repo:<sha>:<candidate-fingerprint>",
  "candidate_id": "github:owner/repo:bug:<sha>:<fingerprint>",
  "repo": "owner/repo",
  "base_sha": "FULL_COMMIT_SHA",
  "mode": "scan|reproduce|repair|review|ci",
  "execution_scope": "broad_scan|project_contribution",
  "policy_snapshot_id": "policy-hash",
  "timeout_seconds": 1800,
  "cpu": 2,
  "memory_mb": 4096,
  "max_estimated_compute_usd": 0.5,
  "model_policy": "free_first",
  "allow_github_write": false
}
```

**不信任 Job JSON 自带的授权值。** `allow_github_write=false` 是 worker 固定特性；任何评论、Fork、Push、PR 都由 VPS 的可信控制器在独立校验后完成。Job 中的 repo、SHA、mode 由控制器从候选记录生成并校验，不能直接接受外部仓库文本指令。

### 4.3 Result（统一输出）

```json
{
  "schema_version": 1,
  "job_id": "job-uuid",
  "backend": "modal|github_actions|local",
  "outcome": "succeeded|inconclusive|failed|blocked|timed_out",
  "reproduced_on_base": false,
  "fixed_on_patch": false,
  "regression_tests_passed": false,
  "patch_path": "artifacts/job-uuid/change.patch",
  "test_report_path": "artifacts/job-uuid/test-report.json",
  "logs_path": "artifacts/job-uuid/worker.log",
  "base_sha": "FULL_COMMIT_SHA",
  "model_used": "provider/model",
  "measured_cost_usd": null,
  "unverified_items": []
}
```

注意 Result 中路径表示可下载的 Artifact 相对路径，不应含未经校验的外部 URL；可信控制器验证 SHA、任务身份、校验和和路径归属后才导入。

## 5. Backend Router：策略规则（确定性代码，不给 Agent 自主决策）

| 模式 | 首选 | 备用 | 备注 |
|---|---|---|---|
| Issue 搜索 / PR 监测 / 轻量 Diff | VPS | 无 | 网络失败按照原逻辑处理 |
| `scan`、跨多个无关第三方仓库 | Modal | 合规的本地/自有 Worker 或暂停 | 不能随意移到公共 Actions |
| `reproduce`，尚未进入某项目的贡献流程 | Modal | 合规其他 Worker 或暂停 | 禁止为了绕过 Actions 限制临时大批量 Fork |
| `repair`，已通过仓库贡献准入 | Actions（项目工作流） | Modal | Actions 无合规工作流时退回 Modal |
| `ci`，针对实际贡献项目 | Actions（项目工作流） | Modal | 最终以上游已有 CI 为准 |
| `review`，针对实际贡献项目 | Actions（项目工作流） | Modal | Review 的 AI 结果不是证明 |

**故障回退条件**：仅运行平台不可用、Runner 配额/环境不符、资源不足等后端问题触发 Modal 回退；不能因为测试断言失败、修复质量差、项目本身有 Bug 就在另一平台无脑重跑。所有回退检查 Modal 预算，不能突破费用保护。

### 统一执行器接口

```python
from typing import Protocol

class ComputeBackend(Protocol):
    def eligible(self, job: dict) -> bool: ...
    def submit(self, job: dict) -> str: ...  # returns remote_job_id
    def poll(self, remote_job_id: str) -> dict: ...
    def cancel(self, remote_job_id: str) -> None: ...
    def collect(self, remote_job_id: str) -> dict: ...
```

公共核心执行器统一入口：

```bash
python -m scout_v2.workers.core --job /workspace/job.json --out /workspace/artifacts
```

可以使用同一版本锁定的 OCI 镜像/执行器源码，但注意 GitHub Actions 与 Modal 的文件挂载、Secrets 和日志/Artifact API 有差异；适配层负责这些差异，**不要承诺环境字节级完全一致**。

## 6. Repository Policy Engine：保留既有所有硬规则

### 强制规则（来自用户提供的现有 Playbook 概述）

- 本地所有读写限于 `G:\project\oss-scout`（Git Bash `/g/project/oss-scout`）。先在此目录读取 AGENTS.md，不得推断其正文。
- 严格按 `AGENTS.md` 第 4 节合规门、第 6 节特殊仓库规则执行。
- **每日本地业务时区至多 1 个新认领**，跨窗口/跨进程/跨 Worker 统一计数；开工先查 `state/worklog.jsonl` 当日是否有 `claim` 或 `awaiting_assignment`，有则仅做 PR 监控及发现，不新认领；已指派的后续检查按 Playbook 处理。
- 认领前实时复核 issue open/closed、assignees、评论、关联 PR、竞争情况；新鲜 Issue 龄 **<2h** 战略放弃。
- 永不 force-push；永不合并上游；上游 403 记录 blocker，不立即重试；保留当前 `gh repo sync xixi-box/<repo>` 等具体步骤的原有正确语义。
- 闸门池 `pydantic/pydantic-ai`、`microsoft/playwright`、`microsoft/agent-framework`：必须先英文技术分析评论、记录 `awaiting_assignment`，等待确实指派给 `xixi-box`，绝不直接创建 PR；不写 "please assign me"。
- 其他仓库必须符合 CONTRIBUTING、Issue/PR 模板、提交消息惯例；缺资料/不确定则 fail-closed，进入人工审核。
- 不完整的修复根据 Playbook 要求进入 Draft PR，并设置 `maintainer_can_modify`；PR 描述包括 `Fixes #issue`（仅确实存在并适用的 Issue）、root cause、implementation、tests、compatibility、unverified items；无 Issue 的 Bug 不得编造 `Fixes` 编号。
- 已开 PR：监控 CI 和维护者评论，因自己改动导致 CI 失败则修复并追加新 commit；Review 意见 48 小时内响应；合并/关闭同步状态；绝不自动合并上游。
- 如仓库有 SECURITY.md 和安全问题私密披露流程，安全漏洞应遵守其非公开披露规则；禁止批量提交未经复现的 AI 猜测 PR。

### 仓库策略配置

按优先级：原 AGENTS.md 及人工批准的仓库例外 > 最新官方仓库贡献规则 > 本地 `repositories.yaml` 保守配置 > LLM 提取建议。若冲突，禁止自动写入、记录冲突、等待人工复核。

```yaml
# configs/repositories.yaml （示例；真实规则须由 agent 审阅 Playbook）
defaults:
  unknown_repo_action: hold_for_review
  auto_comment: false
  auto_pr: false
  min_issue_age_hours: 2
  max_new_claims_per_business_day: 1
repositories:
  pydantic/pydantic-ai:
    lane: assignment_gate
  microsoft/playwright:
    lane: assignment_gate
  microsoft/agent-framework:
    lane: assignment_gate
```

禁止仅凭 LLM 解析结果自动将未知仓库改为 `direct_pr`。本地状态存储使用单写入器或数据库事务 `BEGIN IMMEDIATE` 防重复认领；所有批量 Worker 都要向中央策略引擎申请令牌。

## 7. Issue Hunter：复用现有代码，不做无意义高频刷新

1. 保留原三段流水线接口：`pr_monitor` → `discover` → `consume`，并保留 `state/worklog.jsonl` / `queue/candidates.json` 格式。
2. 新增对重点仓库最近讨论、维护者反馈、历史 Issue 更新、相关 PR 的只读增量观察。
3. 初始建议：广域扫描每 30 分钟；重点仓库增量检查每 10～20 分钟；Health 信息超过 7 天刷新；PR Monitor 约每 1 小时。最终以 GitHub API 配额/现有 Playbook 为准。
4. 使用 ETag/If-Modified-Since（支持时）、分页水位线、SHA/更新时间去重，避免重复扫描；遵守 primary 和 secondary rate limits。
5. 遇到上游 403 按 Playbook 记 blocker，**不要自动尝试绕过或立即重试**；其它可重试限流要尊重 Retry-After 和 API 文档及 Playbook。
6. Issue 搜索只输出候选，不自行 Fork/评论/PR。候选排序考虑维护者响应、外部贡献接受程度、测试成本、是否已有竞争性 PR。

## 8. Bug Hunter：先找线索，再用 Modal 深度验证

### 一级：VPS（轻量、持续）

- 只通过 API 获取 Repo 元数据、语言、变更列表、少量目标文件与已存在测试情况。
- 重点追踪最近频繁修改的边界处理模块、bugfix Diff、回归讨论、文档契约冲突。
- 不在低配 VPS 上执行陌生仓库安装脚本、完整编译、大型测试、全量 Semgrep。
- 控制 GitHub API 请求数量和远程 LLM 调用数量；优先可复现、改动范围小的缺陷。

### 二级：Modal（重型、短时、按需）

1. 任务包含目标 Repo、固定 base SHA、扫描范围和时间/CPU/内存预算。
2. 创建临时 Sandbox，限制运行时长和并发；按需 Clone 目标 Commit（不需提前 Fork）。
3. 运行仓库适用的精选 Semgrep 规则或语言原生 lint/type-check；仅产生可疑线索。
4. 对高价值线索由 OpenCode 只读分析并设计最小失败测试；在**无模型密钥、无 GitHub 写令牌**的隔离测试环境中复现。
5. 只将“原始版失败、预期行为证据明确”的缺陷升级为 `reproduced`；否则 `inconclusive`。
6. 输出候选、复现步骤、测试日志、文件/SHA、模型用量，删除 Sandbox，保存有限期证据。
7. 不进行生产环境漏洞探测、不收集秘密、不上传包含用户数据的仓库内容到不受信任服务。

建议 MVP 扫描 10～20 个中小型 Python 仓库，Modal 同时最多 1 个重任务，每个任务上限建议 15～30 分钟；观测误报率和每个可复现 Bug 的成本再扩展。

## 9. 修复与测试（GitHub Actions 优先）

### 触发前置条件

- 对应项目有明确允许外部贡献的规则，已完成所需的认领/维护者确认；有需要修改的真实 Bug 或被允许的 Issue。
- 若要在自己的 Fork 使用 Actions，必须该 Fork 已存在被允许的工作流并正确启用，或先由人工批准在 Fork 配置项目关联的工作流。**不要为使用 Actions 而提前批量 Fork 未决定贡献的仓库。**
- 如果无合规工作流、Fork 不允许启用、需要资源超出标准 Runner，Actions Adapter 应返回 `ineligible`，然后由 Router 根据 Modal 预算决定转向 Modal 或暂停。

### 执行逻辑

1. VPS 完成 Playbook 准入与实时竞争复检，在符合条件后 Fork/更新 Fork，并准备隔离分支（沿用原 `fix/<issueN>-<slug>` 命名逻辑）。
2. 使用项目工作流 `workflow_dispatch`（需要时可通过 `gh workflow run` 或 GitHub REST API），Job 绑定确定的提交 SHA / 分支；不要由任意 Issue 评论触发具有 Secrets 的代码执行。
3. Workflow 使用**同一份 worker 代码**：执行 `repair`、`review`、`ci` 中允许的模式。
4. 根据项目实际依赖运行相关测试、静态检查、类型检查；实施必要修复与回归测试。
5. 保存 `result.json`、完整测试日志和 `change.patch` 为 Artifact。VPS 控制器拉取并核对 job_id、SHA、补丁变更范围后，再由可信控制器进行 Push / PR（仍需 Playbook 全部合规门）。
6. 已有上游 CI 仍是最终主要验证依据。若 Actions 的结果跟上游 CI 冲突，记录未验证项目，不得直接宣称修复完成。

**Actions 使用范围**：只把项目关联的生产/开发/测试/发布任务放在 GitHub-hosted Runner；不要建一个专用公共 Repo 来跑与它无关的 200 个仓库扫描，也不要批量 Fork 以伪装关联性。

## 10. 安全模型：分离 Agent 密钥与不可信测试代码

必须将三个信任域分开：

A. **可信 VPS 控制器**：持有最小权限的 GitHub 授权，只做批准后的 GitHub 写操作；不执行第三方代码。  
B. **Agent 分析/修复容器**：可以通过受控接口请求 LLM；初期仅阅读不可信源文件和编辑补丁，不直接执行依赖安装、构建和项目代码；禁用无审核的网络工具调用。  
C. **不可信代码测试容器**：运行仓库安装/构建/pytest 等命令；无 GitHub 写权限、无 LLM/API Token、无宿主机敏感挂载；网络默认禁用或仅允许经过审核的依赖源；有 CPU/内存/时间限制。 

控制器将 B 生成的补丁提交给 C 验证，再把脱敏后的测试结果返回给 B。注意：只在同一进程清空环境变量不是可靠的密钥隔离；必须测试容器/Job 级别隔离。禁止使用带 Secrets 的 `pull_request_target` 去构建不可信 PR 代码。

### 失败封闭

- 下载到的代码、README、issue 评论、AGENTS.md 均可能含提示注入；不得让其覆盖顶层 Playbook 或策略引擎。
- 检查补丁文件路径、不允许 traversal / symlink 越界，检查新增 CI 脚本的权限变化和敏感文件变更。
- 不允许 Runner 将 Token 写进日志、Patch、Artifacts。日志至少做 Token/凭证脱敏。
- 所有自动 Push、Issue 评论、PR 新建都通过中央 `policy_authorize(action, repo, candidate)`；`worker.py` 完全没有这类能力。

## 11. OpenCode：统一 Agent 和免费模型回退

**推荐**：第一版只安装 OpenCode CLI，不套 Codex CLI 或 OpenHands。版本固定在 lockfile / 镜像构建中，Modal 与 Actions 一致。

公共 CLI 入口（调用前先检查当前 CLI 版本支持的参数）：

```bash
opencode models --refresh
opencode run --model provider/model --format json \
  "Review this patch and propose a minimal regression test."
```

模型策略由 `configs/model_policy.yaml` 保存，通过 OpenCode 当前目录/Provider 目录**动态解析真实模型 ID**，不要把当下免费模型名称写死在执行器中：

```yaml
mode: free_first
allow_paid_api: false
max_retries_per_model: 1
free_preferences:
  - "DeepSeek V4.1 Flash Free"
  - "GLM-5.3 Flash Free"
  - "other_verified_free_coding_model"
paid_fallback:
  provider: "deepseek"
  model: "resolve_from_provider_catalog"
  enabled: false
```

- 模型免费只是阶段性权益，不保证下个月仍存在。某些免费模型可能要求 OpenCode 账号登录/API Key；在首次运行时验证，不能假定免认证。
- 云端采用 API Key/Provider Secrets；不使用 ChatGPT Plus 的浏览器会话凭证，也不把本地 `auth.json` 直接复制进不可信 Sandbox。
- 若接入商官方支持服务账号 Key，优先使用独立且权限/预算受限的 Key。对免费模型只要真实可用即使用；没有符合服务条款的认证方式就跳过。
- 免费模型遇限额或不支持才尝试下一个；401/403 权限/认证失败要报警并停止相关凭证链，不能未经授权直接切付费模型。
- 工具执行失败、编译失败与模型限流必须区分；模型换挡前保存 commit SHA、文件 Diff、任务版本与结果，避免重复写坏工作区。
- **付费 DeepSeek 默认关闭**。只有明确批准并设置可验证的预付额度、单任务预算、月预算后才启用；无法验证剩余预算时 fail-closed。
- 免费模型最终失败：暂停任务而非默默调用付费接口。

## 12. Modal 费用保护（必须先于任何实际运行上线）

- Modal 官方 Starter 价格页目前列 **$30/月 included compute**，不等于无限次免费，也不包含其他供应商的 LLM 费用。
- 在 Modal Usage & Billing 中验证 Workspace **Spend Limit = $0（若账户允许）**，并设置保守的 Workspace Budget（建议 $25/月）；无法确认平台级零自付上限时，**禁止启动收费资源**，不要以脚本代替硬限制。
- 使用 `modal billing summary --json` 或官方 SDK 获取当月计量成本；先实测账号权限和返回结构，避免擅自假定字段一直不变；读数延迟会出现。
- 启动每个任务前预留 `estimated_max_cost = max_runtime * max_resource_rate`，考虑并发中的已承诺成本；只有 `metered_usage + reservations + new_job_upper_bound <= monthly_soft_cap` 才创建。
- 每次最多 1 个分析 Sandbox；最长运行时间、CPU、内存有上限；预算查询失败、API 故障、费用不可解释则拒绝创建新任务。
- 一旦接近 soft cap 或平台硬上限，暂停 `broad_scan` / 独立 `reproduce` 等任务直到额度刷新；保留 VPS 的 Issue Hunter 与轻量分析。对合规的项目 CI，Actions 仍可继续。
- Modal 的 $0.50 账户验证/退款不可假定；按账户实际账单和官方客服确认。不要暗中替用户完成任何付款。

## 13. 状态管理、幂等、并发

**重要：不破坏原有审计数据。** 迁移时保持 `state/worklog.jsonl` 可读、可审计；新增 Job 状态持久化可以采用 `state/scout_v2.sqlite3`，但只能由单个可信服务写主状态，不能同时让 JSON 与 SQLite 独立决定认领。

建议：

- `job_id` 全局唯一；`idempotency_key` 基于 repo + SHA + issue/bug fingerprint + mode + policy version。
- `queued → leased → running → collecting → succeeded|failed|inconclusive|blocked`；Lease 过期由回收任务处理，不直接重复提交 GitHub 写操作。
- 候选生命周期：`discovered → triaged → awaiting_policy|eligible → analyzing → suspected → reproduced → ready_for_contribution → ...`。GitHub PR 的 `pr_open / merged / closed` 保留旧语义。
- “每日新认领”使用配置的**现有业务时区**，在一次事务中核查 `worklog` 当日 `claim/awaiting_assignment` 与新锁，拒绝并发重复占额。不能自行把日期语义改成服务器 UTC。
- 每个 worker 结果都可重复上传，但最终状态写入幂等。任何 `403` 写操作按 Playbook 记 blocker，不进入无限重试。
- 若在启动后发现 Issue 被关闭、维护者改规则、出现重复 PR，取消候选的新 GitHub 写动作；已生成的复现材料可留作内部分析。

## 14. 调度方案（24/7 是持续待命，不是 24 小时满载调用模型）

初始建议：

| 任务 | 周期 | 资源 |
|---|---|---|
| 重点仓库 Issue/评论监控 | 15 分钟 | VPS API |
| 广域 Issue 搜索 | 30 分钟 | VPS API |
| PR 评论/CI 检查 | 60 分钟 | VPS API |
| 轻量 Bug 线索提取 | 4～6 小时 | VPS API + 少量模型 |
| Modal 深度 Bug 验证 | 候选触发，单并发 | Modal |
| 健康评分更新 | 超过 7 天 | 现有 `health_check.py` |
| 贡献消费 | 先沿用 10:30/16:30/22:30 | 原流水线 |

**迁移原则**：先不改原三个消费窗口；等并发锁、状态兼容、仓库策略、审计测试全部通过后，提供 `continuous_consume=false` 配置开关，经人工审阅 Playbook 更新后才能切为“合规候选出现即触发消费”。即使改为全天候，仍保留每日最多 1 次新认领。

VPS 资源守门：主进程常驻尽量 <200～300 MB RSS（目标，不是承诺），重任务不本地执行，计划单进程轮询/队列；持续记录可用内存、CPU 峰值、OOM、API rate remaining，允许动态降频。

## 15. 逐步实施顺序（每阶段可独立验收）

### P0：只读审计与回滚计划（必须先做）

- 在 `G:\project\oss-scout` 下读取 AGENTS.md 全文、目录树、依赖锁、所有 `state` 和 `queue` 相关代码。
- 提取硬规则清单与实现位置；核查是否已经有 file lock/DB、定时器、Queue、分层评分、PR 监控。
- 创建只在项目目录内的本地快照/备份或 Git 分支，列出不会被改变的外部资源。
- 输出差异化实施清单和预计新增模块；不进行 GitHub 评论、Fork、Push、PR、云资源创建和付款。
- **验收**：报告现有流程与新方案的实际冲突；每条硬规则有明确代码/测试映射。

### P1：统一 Schema / Queue / Policy（本地完成）

- 建立候选、任务、结果 Schema 与序列化兼容；增加幂等、并发文件锁或 SQLite 事务。
- 原有 `discover.py` 能继续运行；新增只读扫描模式，原有历史 worklog 可完整读取。
- 写单元测试覆盖每日认领并发、时区跨天、历史日志兼容、未知仓库、指派闸门、fresh issue、403 blocker。
- **验收**：双进程/双线程竞争当日最多一条新认领动作；现有脚本测试全绿，历史数据不丢。

### P2：Modal Adapter + Bug Hunter MVP（默认 Dry Run）

- 先 Mock Modal，再做可手动启用的沙盒 smoke test；在账户零自付硬限制未确认前禁止实际执行。
- 只使用经过人工选定的 3～5 个小型 Python 开源项目；验证 Semgrep → 可疑线索 → 最小复现 → Result JSON。
- Worker 不具备 GitHub 写 Token；对外部项目的测试代码使用秘密隔离执行环境。
- **验收**：任务可中止并正确回收，产出可复现/不可复现的明确证据，预算近阈值时不启动。

### P3：Actions Adapter + 通用 Worker

- 先在**自己的测试项目或确实处于贡献流程的项目**中运行 `workflow_dispatch`，不使用不相关公共 Repo 来打包批量扫描。
- 验证同一版 Worker 分别在 Modal 与 GitHub Actions 生成等价的 `result.json` / Patch / 测试报告；接口不依赖平台特性。
- Actions 无法运行/没有合规 workflow 时返回 `ineligible`，按路由自动转 Modal（仅限预算内）或暂停。
- **验收**：项目内单元测试成功，平台失败不导致重复 PR 或重复消耗；Artifact 校验生效。

### P4：OpenCode 模型路由（免费优先）

- 验证当前实际可用免费模型列表和账号认证；构造 Fake Provider 测试模型限流、认证失败、下线和 5xx。
- 免费模型按序尝试；关停所有无授权付费兜底；单次会话切换保留完整工作状态。
- **验收**：免费模型全部失败时总额外付费 = 0；Secrets 不出现在测试环境/产物。

### P5：完整贡献闭环（谨慎上线）

- 输入一个有代表性的、经过允许的小 Issue 或内部测试仓库 Bug；确保合规门完整通过后才允许外部写入。
- 本地 Playbook 支配 Fork、分支、修复、测试、PR、review 监控、状态与飞书通知。
- **验收**：无权限不评论/不 PR；Review/CI 失败可追踪、可恢复；上游 403 blocker 不重试。

### P6：试运行和持续消费选项

- 至少 7 天只读/模拟模式积累候选、重复率、可复现率、Worker 费用和失败分布。
- 再决定是否修改 Playbook，将三固定消费窗口升级为事件驱动；默认不启用。
- **验收**：VPS 现有业务不受影响，24h 可重启恢复，系统超限自动暂停，人工一键退回旧三窗口模式。

## 16. 核心测试清单（Agent 必须补齐）

- `AGENTS.md` 不存在/解析失败 → 停止自动 GitHub 写入。
- 同一日已有 `claim` / `awaiting_assignment`，三个窗口和所有异步 Worker 都不能新增认领。
- 闸门池无 assignee / 维护者未确认 → 即使 Bug 已复现也禁止 PR；禁止 "please assign me" 文案。
- issue 创建未满 2h → 不认领；issue 已关闭或已有 PR → 复检停止。
- 不允许 force push；不允许合并上游；`403` 写操作 blocker。
- 仓库贡献规则更新 / policy SHA 变化 → 原授权无效，重新审核。
- Modal 计量已达软阈值、账单数据不可用、硬上限未确认 → 不创建 Sandbox。
- GitHub Actions task.scope 为 `broad_scan` → 一律拒绝路由；无合规 workflow / 私有付费风险 → ineligible。
- 免费模型 402/429/不可用 → 有限回退；401/403 认证错误 → 报警，默认绝不自动付费。
- base SHA、result SHA、artifact hash 不一致 → 禁止导入 Patch/Push。
- 不可信安装脚本无法读取 Modal / OpenCode / GitHub Tokens。
- 原始版测试失败、补丁版测试成功且其他相关测试通过，才标识 reproduced/fixed；否则不宣称已修复。
- PR 已合并/关闭、Review 超时、CI 失败、任务超时、Worker 取消均写入 Worklog 与日报。
- 原有 `discover.py` 单独运行逻辑与 `health_check.py` 7 天刷新保持可用。

## 17. 运维与费用审计

- 飞书仅推送：新 A 级候选、已复现 Bug、外部 GitHub 写动作、PR 审查/CI 失败、预算不足/认证失败、异常停止；不要逐条推送每个无价值扫描结果。
- 日报：`state/runs/YYYY-MM-DD.md`（存在则追加）；单次扫描日志保留在原 `state/runs` 中。
- 费用维度拆开：`modal_metered_usd`、`modal_billed_usd`、`actions_minutes`、`llm_tokens_by_model`、`llm_paid_usd`、`vps_load`。
- PR 指标：有效候选率、真实复现率、单个复现 Bug 成本、PR 审查通过率、最终合并率（排除仍待审 PR）、维护者首次响应时间、重复/不适用的失败原因。
- 不上传密钥、不在日志打印凭证；不要把抓取的仓库敏感文件和巨量日志推送飞书。
- 文档中记录停机/切换命令、凭证轮换和账单平台控制台路径；通过模拟演练验证回滚。

## 18. 给本地 Agent 的最终执行要求

1. 首先 `cd /g/project/oss-scout`，查看 AGENTS.md 全文并审计现有项目。
2. 所有**本地文件读写**限于该项目目录；不得改变其他盘符和全局配置，不安装系统服务或云端资源作为第一步。
3. 先列出逐步实施清单，优先创建 P0/P1 的最小必要代码及单元测试；不重复造轮子，不无故重构现有流水线。
4. 未获得进一步明确授权之前，所有云服务启动、付费、账户验证、GitHub 评论/认领/Push/PR 均保持模拟/只读模式。
5. 若发现本计划与 `AGENTS.md` 冲突，以 `AGENTS.md` 为准，不自行修改硬约束，只写冲突报告。
6. 完工后提供：新增和修改的文件清单、实际执行的测试命令与结果、未验证功能、运行方式、必要的 Secrets 清单（仅名称，不显示值）、下一阶段需要人工完成的授权动作。

## 官方参考资料（实施前复核）

- GitHub Actions 附加条款： https://docs.github.com/en/site-policy/github-terms/github-terms-for-additional-products-and-features
- GitHub-hosted Runner 规格： https://docs.github.com/en/actions/reference/runners/github-hosted-runners
- GitHub Actions Billing： https://docs.github.com/en/actions/concepts/billing-and-usage
- Workflow Dispatch REST API： https://docs.github.com/en/rest/actions/workflows
- Actions 不可信 PR 代码安全： https://docs.github.com/en/actions/reference/security/securely-using-pull_request_target
- Modal Pricing： https://modal.com/pricing
- Modal Budgets/Spend Limits： https://modal.com/docs/guide/budgets
- Modal Billing CLI： https://modal.com/docs/cli/latest/billing
- Modal Sandboxes： https://modal.com/docs/guide/sandbox
- OpenCode CLI： https://opencode.ai/docs/cli
- OpenCode Models： https://opencode.ai/v2/docs/models
- OpenCode Providers： https://opencode.ai/docs/providers
- OpenCode Console Models（免费模型会变化）： https://opencode.ai/v2/docs/console/models

