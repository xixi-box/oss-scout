# P0 审计报告：现有系统 vs oss-scout v2 实施计划

日期：2026-10-24 ｜ 审计人：本地 Coding Agent ｜ 依据：`AGENTS.md`（Playbook）+ 全量代码/状态阅读

## 1. 现有资产盘点

| 资产 | 状态 | 说明 |
|---|---|---|
| `discover.py`（371 行） | 健康 | 纯 `gh api` 只读发现；三路搜索（reactions / 新 bug / 2~14 天新鲜泳道）；信号正则 + A/B/C 粗分；原子写 JSON；飞书通知可选 |
| `health_check.py`（149 行） | 健康 | 75 天窗口外部 PR 生态健康度 + CONTRIBUTING 政策闸扫描 + lane_advisory |
| `config.json` | 健康 | 15 open + 3 gated + 1 restricted + 1 observe-only 仓库，含每仓库特殊规则 |
| `state/worklog.jsonl` | 健康（258 行） | 唯一审计账本；**注意：混有非 oss-scout 动作**（Clash Verge 修复、UI 截图分析等），v2 不得过滤或改写 |
| `queue/candidates.json` | 受损后已重建 | 2026-10-22 曾发生 0x00 填零损坏（NTFS 预分配 + 进程中断），现全部为 `queued_hist` 重建条目；旁有 `candidates.json.corrupt-20261022` 原始损坏件 |
| `state/seen.json` | 健康 | 发现层去重，discover.py 独占写 |
| `state/health.json` | 陈旧 | 计算于 2026-10-12（>7 天，按 Playbook 需重跑） |
| `state/claim_window.json` | 数据异常 | 全部 repos 显示 issues=0，疑似测量工具受 TLS 抖动影响未取到数；claim-window 测量任务被用户暂停（见 worklog 2026-10-24T20:14） |
| `state/*.ps1`, `tmp_*.json`, `goose_ext.tsv`, `pr5707_body.md` | 杂物 | 历史调查产物，保留不动 |
| `repos/`, `worktrees/` | 存在 | 懒 clone 缓存与 worktree，快照已排除（见 `.gitignore`） |

备份：`git init` 于项目目录内，P0 提交 `08d16e0`（189 文件，排除 repos/worktrees/__pycache__）。

## 2. Playbook 硬规则 → v2 代码映射

| 硬规则 | v2 实现位置 | 测试 |
|---|---|---|
| 每日至多 1 个新认领（跨窗口/跨进程） | `scout_v2/state_store.py::ClaimLedger`（SQLite `BEGIN IMMEDIATE` + worklog 当日扫描双闸） | `test_quota_concurrency.py` |
| 认领前实时复检（open/assignees/竞争/关联 PR） | `scout_v2/git_host.py::recheck_candidate`（只读） | `test_router.py`（mock） |
| 龄 <2h 新鲜 issue 放弃 | `scout_v2/repo_policy.py::check_freshness` | `test_policy.py` |
| 永不 force-push / 合并上游 / 跳过 CI | `repo_policy.py::authorize()` 动作白名单，`push_force`/`merge_upstream` 一律拒绝 | `test_policy.py` |
| 上游 403 → blocker，本轮不重试 | `state_store.py::BlockerRegistry` + policy 拒绝重复写 | `test_policy.py` |
| 闸门池不直接开 PR、绝不 "please assign me" | `repo_policy.py` lane=assignment_gate 时 `open_pr` 拒绝；认领评论模板白名单校验 | `test_policy.py` |
| 未知仓库 fail-closed | `repositories.yaml` `unknown_repo_action: hold_for_review` | `test_policy.py` |
| AGENTS.md 缺失/不可解析 → 停止一切写 | `repo_policy.py::PolicyEngine.load()` fail-closed | `test_policy.py`, `test_security.py` |
| policy SHA 变化 → 旧授权失效 | Job 携带 `policy_snapshot_id`，控制器比对当前 hash | `test_router.py` |
| base SHA / artifact 校验不一致 → 禁止导入 | `workers/quality_gate.py::verify_result` | `test_security.py` |
| Modal 软阈值/账单不可读 → 不创建 Sandbox | `scout_v2/budget.py` fail-closed | `test_budget.py` |
| broad_scan 禁走 GitHub Actions | `job_router.py` 硬规则 | `test_router.py` |
| 免费模型失败有限回退；401/403 报警停链；付费默认关 | `workers/model_fallback.py` + `configs/model_policy.yaml` | `test_backends_mock.py` |
| worklog/candidates 历史兼容 | `scout_v2/candidate_normalizer.py` + `state_store.py::Worklog`（append-only） | `test_worklog_compat.py` |

## 3. 计划与 Playbook 的实际冲突（按 Playbook 为准，v2 已让步）

1. **调度频率**：计划 §14 建议"重点仓库 15 分钟/广域 30 分钟"，但 Playbook §1.5 规定观测任务每 2h、消费仅三窗。→ v2 `configs/v2.yaml` 默认沿用 Playbook 节奏，更激进频率仅作为注释选项，切换需人工改 Playbook。
2. **连续消费**：计划允许 `continuous_consume`，默认 `false`，与 Playbook 三窗一致。
3. **时区**：worklog 时间戳为本地裸时间（naive local）；计划要求"现有业务时区"计日。→ 配置项 `business_timezone: Asia/Shanghai`（**待用户确认**），ClaimLedger 用该时区切"当日"，不得偷用 UTC。
4. **SQLite 与 JSON 双状态**：计划允许 SQLite 做主状态。→ P1 裁定：候选真值仍是 `queue/candidates.json`（discover.py 兼容），SQLite 只管 Job/租约/认领锁，不裁决认领事实（认领事实以 worklog 为准，SQLite 锁只是并发闸）。
5. **写损坏教训**：计划未提，但 10-22 的 0x00 损坏实证了 `atomic_write` 在本机不可靠。→ v2 统一写路径 `state_store.py::atomic_json_write` = 临时文件 + fsync + replace + 回读解析校验 + 滚动 `.bak`。

## 4. P1 后仍待人工/后续阶段的事项

- claim-window 测量（`state/claim_window_measure.py`）与 health.json 重跑：用户已暂停，待恢复。
- P2 需要：Modal 账号、Workspace Spend Limit 确认为 $0/预算、3~5 个 MVP 目标小仓库名单。
- P3 需要：一个属於用户、可放 `workflow_dispatch` 工作流的测试仓库。
- P4 需要：OpenCode CLI 安装与免费模型登录态验证。
- 本环境为 Windows 本地开发机，计划的目标运行环境是 Linux VPS；`docker/` 与 systemd 相关产物 P1 只交付模板，不做运行验证。
