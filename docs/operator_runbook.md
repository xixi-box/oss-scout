# Operator Runbook

## 日常

- **测试**：`python -m pytest tests_v2 -q`（当前 89 项，全绿基线）。
- **观察调度计划**（不启动进程）：
  `python -c "import yaml;from scout_v2.scheduler import scheduled_plan;print(scheduled_plan(yaml.safe_load(open('configs/v2.yaml',encoding='utf-8'))))"`
- **写状态文件**：一律走 `scout_v2.state_store.atomic_json_write`，不要手写 `json.dump`。
- **认领动作**：先 `PolicyEngine.authorize("claim", repo)`；通过后
  `ClaimLedger.try_claim(tz, candidate_id)` + `Worklog.append({"ts":…, "action":"claim", …})`。
- **403 写失败**：`BlockerRegistry.register(f"{repo}:{action}", err)`，本轮不再重试（自动生效）。

## 启停 / 回滚

- v2 全部功能默认 dry-run/disabled（见 `configs/v2.yaml` features）。
- 一键回滚 = 继续按原 Playbook 跑 `discover.py + health_check.py + 三窗消费`；
  v2 新增文件（scout_v2/、tests_v2/、configs/、docs/、docker/）不影响原路径。
- 历史状态恢复：`queue/candidates.json.bak` 为上一版；`candidates.json.corrupt-20261022`
  为 0x00 事故原始件（只读留存）。

## 数据库

- `state/scout_v2.sqlite3`：JobStore / ClaimLedger / BlockerRegistry（WAL 模式，单写者）。
  损坏时可直接删除重建——它不是认领事实来源（worklog 才是），重建只丢并发锁不丢历史。

## 费用

- Modal：`budget.enabled=false` 直至确认 Workspace Spend Limit；软阈值 $25/月。
- OpenCode：`allow_paid_api=false`；免费模型失败 → 任务暂停，绝不自动付费。
- Actions：`actions.enabled=false` 直至 P3 且仅在项目关联工作流使用。

## 凭证清单（名称，不含值）

- `GH_TOKEN` / `gh auth`：只读发现 + 控制器写操作（控制器侧）。
- `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET`：P2 起，仅控制器持有。
- OpenCode provider key：P4 起，容器级注入，绝不复制 `auth.json` 进测试容器。
