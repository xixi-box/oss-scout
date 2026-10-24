# 迁移计划（旧三段流水线 → v2）

## 原则

1. 不删任何旧文件；旧流水线始终是回滚目标。
2. 每阶段有独立验收（计划 §15），验收不过不进下一阶段。
3. 所有对外写动作在获得人工授权前保持 disabled/dry-run。

## 阶段状态

| 阶段 | 内容 | 状态 |
|---|---|---|
| P0 | 审计 + git 快照（commit `08d16e0`）+ 冲突报告 `docs/p0_audit.md` | ✅ 完成 |
| P1 | Schema/队列/策略/预算/路由/守门 + 89 项测试 | ✅ 本仓库已交付 |
| P2 | Modal adapter + Bug Hunter MVP（默认 dry-run） | ✅ 代码完成（平台硬限制已确认：usage limit $30 / spend limit $0）；待用户 `modal token set` 后跑 `python scripts/modal_smoke.py --yes` 实测 Sandbox 链路 |
| P3 | Actions adapter + 通用 worker 真实执行 | ⬜ 需用户的测试仓库 |
| P4 | OpenCode 模型路由实测 | ⬜ 需安装 OpenCode CLI + 免费模型登录验证 |
| P5 | 完整贡献闭环（谨慎上线） | ⬜ |
| P6 | 7 天只读试运行 → 事件驱动消费（默认不启用） | ⬜ |

## 数据迁移语义

- `state/worklog.jsonl`：只追加，永不改写；v2 ClaimLedger 按业务时区读它计认领。
- `queue/candidates.json`：仍由 discover.py 与 v2 共享；写路径升级为
  `atomic_json_write`（校验 + `.bak`），旧条目经 `candidate_normalizer` 透传。
- `state/scout_v2.sqlite3`：新增，可随时删除重建。
- `state/seen.json`：发现层专属，v2 不写。

## 切换到事件驱动消费（P6，默认关闭）

前置：并发锁、状态兼容、仓库策略、审计测试全部通过 + 人工修订 Playbook。
方法：`configs/v2.yaml` 的 `scheduler.consume_windows` 改注释项并
`continuous_consume: true`——`scheduler.scheduled_plan()` 在 Playbook 未修订时会拒绝该配置。
