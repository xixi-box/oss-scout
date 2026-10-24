# oss-scout v2 架构

## 1. 组件与数据流

```text
调度（scheduler.py，P1 仅守门，不常驻）
  ├─ 观测窗口（每 2h，仅 discover.py）         ← Playbook §1.5 节奏
  ├─ 消费窗口（10:30/16:30/22:30 三段）        ← 原流水线
  └─ health 周检（>7 天）
            │
            ▼
候选（queue/candidates.json = 真值，JSON 原子写+校验+.bak）
  └─ candidate_normalizer.py：旧结构 ↔ v2 Candidate（schema 兼容层）
            │
            ▼
策略引擎（repo_policy.py）── 授权闸 policy_authorize(action, repo, candidate)
  ├─ AGENTS.md fail-closed（缺失/不可解析 → 全部写拒绝）
  ├─ 车道：open / gated / restricted / observe / unknown(hold)
  ├─ 每日 1 认领：worklog 当日扫描 + SQLite ClaimLedger（BEGIN IMMEDIATE）双闸
  └─ 快照 policy_snapshot_id：AGENTS.md+策略 hash，变化 ⇒ 旧授权失效
            │
            ▼
路由（job_router.py，确定性规则表）
  ├─ scan/broad_scan → 仅 Modal（Actions 永拒）
  ├─ reproduce（未准入）→ 仅 Modal；不为绕 Actions 批量 Fork
  └─ repair/review/ci（project_contribution）→ Actions 优先 → Modal（预算内）→ 暂停
            │
            ▼
执行（workers/core.py，统一入口，同一份代码跑 local/Modal/Actions）
  ├─ 预算闸（budget.py）：fail-closed，账单不可读 = 不创建
  └─ 产出 Result + patch + test-report（artifact 相对路径）
            │
            ▼
可信控制器导入（workers/quality_gate.py）
  └─ job_id/SHA/hash/路径校验 → 通过后才允许 GitHub 写（且仍过 Playbook 全部合规门）
```

## 2. 关键裁定（与计划文本的差异，依据 AGENTS.md 优先）

| 主题 | 裁定 |
|---|---|
| 候选真值 | `queue/candidates.json`（discover.py 兼容）；SQLite 只存 Job/租约/认领锁/403 blocker |
| 每日认领计数 | 事实以 `state/worklog.jsonl` 为准；SQLite 锁只解决并发竞争 |
| 业务时区 | `configs/v2.yaml: business_timezone`（默认 Asia/Shanghai，待确认）；worklog 裸时间戳按该时区语义切日 |
| 调度频率 | 默认 = Playbook；scheduler.scheduled_plan() 对 <2h 观测或连续消费直接抛错，须先修 Playbook |
| 原子写 | 临时文件 + fsync + 替换前备份旧版到 `.bak` + 落地后回读校验（0x00 事故对策） |
| Worker 权限 | 恒无 GitHub 写令牌；`allow_github_write` 恒 false，core 入口二次拒绝 |

## 3. 信任域（计划 §10）

- **A 可信控制器**（VPS）：唯一 GitHub 写者；不执行第三方代码。
- **B Agent 容器**：P3 起接入 OpenCode；只读分析 + 产补丁，不装依赖不跑测试。
- **C 测试容器**：跑安装/构建/pytest；无 token、无网络（或白名单源）、有 CPU/内存/时限。
- B 的补丁交给 C 验证；三域隔离在 Job/容器级，不做进程内"清环境变量"伪装。

## 4. 模块索引

| 文件 | 职责 |
|---|---|
| `scout_v2/schemas.py` | Candidate / Job / Result 契约 |
| `scout_v2/state_store.py` | atomic_json_write、Worklog、ClaimLedger、BlockerRegistry、JobStore |
| `scout_v2/repo_policy.py` | PolicyEngine + authorize() + freshness + 快照 hash |
| `scout_v2/budget.py` | BudgetGuard（Modal 软阈值/预留/并发/fail-closed） |
| `scout_v2/job_router.py` | 确定性路由矩阵 |
| `scout_v2/git_host.py` | 只读 gh api + 认领前实时复检（无写函数） |
| `scout_v2/candidate_normalizer.py` | 新旧候选结构互转 |
| `scout_v2/scheduler.py` | 调度计划 + Playbook 冲突守门 |
| `scout_v2/workers/core.py` | 统一执行入口 `python -m scout_v2.workers.core` |
| `scout_v2/workers/quality_gate.py` | 结果导入校验（SHA/路径/hash/证据链） |
| `scout_v2/workers/model_fallback.py` | 免费模型回退；401/403 停链；付费 fail-closed |
| `scout_v2/adapters/*` | local（可跑）/ modal（P2）/ actions（P3） |
