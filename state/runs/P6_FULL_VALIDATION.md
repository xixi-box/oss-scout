# P6 全链路真实验证报告

日期：2026-10-25 ｜ 执行：本地 Coding Agent ｜ 环境：Windows 11 / Python 3.14 / gh 2.98 / modal 1.6.2 / opencode 1.18.27

**结论先行**：系统各处理阶段（发现 → 静态扫描 → AI 分析 → 修复 → 回归 → PR → CI）全部在真实环境中跑通。Modal Sandbox 扫描 3 个真实第三方仓库得到 14 条静态线索，经 OpenCode 分析均为误报——按规则不标记任何"已复现真实 Bug"，Bug Hunter 的有效性（而非功能性）验证仍待 Shadow Run 积累。本机 TLS 间歇抖动是全程最大的环境干扰源，已通过有限重试消化。

## 1. Issue Hunter（真实 GitHub 数据）

| 项 | 结果 | 判定 |
|---|---|---|
| discover.py 全量真实运行 | 19 个监控仓库全量扫描完成（耗时 ~45 分钟，TLS 退避拖慢；日志 `state/runs/discover_20261025_0110_p6.log`），**发现 1 个 A 级新候选**：anomalyco/opencode#56297（已入队） | 真实验证 |
| 候选写入/去重/排序 | 机制经 P1~P5 单测覆盖 + 历史数据（91 条）经 v2 层完整可读 | 真实验证 |
| 空跑处理 | 今日 0 新候选时按 Playbook 记录空跑，不伪造 | 真实验证 |
| API 限流/403/超时 | gh 重试 + 25s 退避触发多次（ragflow 等），无崩溃 | 真实验证 |

## 2. Bug Hunter（真实 Modal Sandbox）

| 项 | 结果 | 判定 |
|---|---|---|
| Sandbox 创建/销毁/拉仓/semgrep | 3 仓全成功：openai-agents-python（5 线索/34s）、browser-use（7/62s）、langsmith-sdk（2/55s） | 真实验证 |
| OpenCode 只读分析 | 3/3 成功，结论均为 **false positive**（各附"定案测试"建议） | 真实验证 |
| 已复现真实第三方 Bug | **0** —— 按规则如实记录，不计任何假阳性 | 真实空结果 |
| 上游去重搜索 | 前 N 线索已带 upstream issue/PR 搜索结果（state/bughunt/<repo>/<sha>/findings.json） | 真实验证 |
| Modal 实际计量 | 起始 $0.0101 → 结束 **$0.0301**（3 次扫描 + 前期调试）；远低于 $25 软阈值/$27 停止线 | 真实验证 |

证据文件：`state/bughunt/p6_summary.json`、`state/bughunt/<repo>/<sha>/`、`state/runs/p6_bughunter_*.log`

## 3. GitHub Actions Worker

| 项 | 结果 | 判定 |
|---|---|---|
| workflow_dispatch 触发 | run `40115116266`（smoke.yml, job_id=p6-actions-1）→ success | 真实验证 |
| 结果回传 | stdout 信封收集 + job_id 校验 PASS（artifact 通道仍被账号存储配额阻塞，走日志信封降级） | 真实验证 |
| 密钥泄露检查 | 信封与日志无 token（tests_dashboard 打码测试 + 核对） | 真实验证 |
| 与 Modal 结果格式 | 同一 Result schema / 同一信封协议 | 真实验证 |

## 4. Canary 端到端（自有仓库）

发现候选（埋 bug）→ 策略门（push/open_pr/claim 全放行）→ 隔离克隆 → base 复现失败 → OpenCode(deepseek-v4.1-flash) 修复 → 回归通过 → 质量闸 PASS → [PR#3](https://github.com/xixi-box/oss-scout-v2-test/pull/3) → CI（pr-ci pytest pass 13s）→ 合并。

工作区清洁性：TASK.md / opencode.json / __pycache__ 均未进入补丁（PR#1 教训后已加 .gitignore + 暂存区剔除，PR#2/#3 干净）。

**人工埋设 Bug 的成功率不计入第三方真实 Bug 发现统计。**

## 5. 实测修复的缺陷（全部有测试或证据）

| 缺陷 | 修复 |
|---|---|
| ModalSandboxBackend 用了不存在的 `timeout_seconds` 参数 | 核对真实签名改 `timeout`；`from_dockerfile` 补 `context_dir` |
| 镜像 ENTRYPOINT 吞掉 Sandbox 命令参数（rc=2） | 改走 `OSSCOUT_JOB_B64` 环境变量通道 |
| `/workspace` 归 root，worker 写不了 artifacts | Dockerfile mkdir+chown |
| `/entrypoint.sh` 无执行位 + CRLF | `COPY --chmod=0755` + `sed 去 \r` |
| sandbox root 跑 git 报 dubious ownership | `git config --global --add safe.directory` |
| 部分 clone 取不到目标 SHA | fetch 回退 + stderr 回传 |
| opencode TLS 证书抖动 | 网络类错误有限重试（401/403 不重试） |

## 6. Dashboard（P7）

20 项测试全绿 + 真实数据冒烟：7 页全 200、路径穿越 400、token 打码、gh/Modal 失败显示"未知"、POST 一律 501、只读打开 sqlite。Wise 风格（DESIGN.md）+ HTMX 20s 轮询。启动：`python -m dashboard.app` → http://127.0.0.1:8787（仅监听 127.0.0.1）。

## 7. P6-C Shadow Run

`scripts/shadow_stats.py` 首日快照已落盘（`state/shadow/2026-10-25.json`）；每日 09:30 自动统计已注册（共 7 次，只读）。原消费窗口与每日认领规则不变。

## 8. 明确区分

- **真实验证成功**：Modal Sandbox 链路、Actions 链路、Canary 修复闭环、CI 监控、OpenCode 调用、策略门、Dashboard 真实数据渲染。
- **Mock 成功**：路由矩阵、预算闸边界、并发认领锁、模型回退链（单测层）。
- **未执行**：事件驱动消费（默认关闭，需 Playbook 修订）、公网暴露（需安全访问层）。
- **失败（已修复）**：上表 7 项实测缺陷。
- **被规则阻止**：外部开源仓库的评论/认领/PR（Shadow Run 期间禁止）。

## 9. 阻塞与未验证项

1. **GitHub Actions artifact 存储配额已满**（账号级）：大补丁传输需清理旧 artifacts，当前走日志信封（~64KB 上限）。
2. **真实第三方 Bug 复现数为 0**：Bug Hunter 只能宣称"功能链路可运行"，有效性待 Shadow Run（更宽规则集 + 复现任务接入）。
3. health.json 已过期（>7 天），重跑被用户暂停中。
4. review/ci 两种 worker 模式仍是骨架。
5. discover.py 今日全量扫描耗时 40+ 分钟（TLS 退避放大），需在 VPS 环境复测基线。
