# oss-scout

全自动开源贡献流水线：扫描监控仓库的新 issue → AI 判断能否自动修 → 在 GitHub
Actions 上跑 opencode 修复并验证 → 以你的名义开 PR → 盯住后续的 CI 和评论，
该修的修、该回的回。

## 一图看懂当前工作流

```
┌─ 发现层（每分钟）─────────────────────────────────────────────┐
│ Cloudflare Worker 扫描 50 个监控仓库的新 issue                │
│   → POST /api/ingest/issues（token 鉴权）                     │
│   → 客观否决：已有 PR / 纯文档 / 计费抱怨 → 直接拒绝          │
│   → 粗筛通过才做 OpenJev 细筛，暂存或手动消费时用最新证据重筛 │
└──────────────────────┬───────────────────────────────────────┘
                       ▼
┌─ 认领层（额度真实有效）───────────────────────────────────────┐
│ 每日最多成功创建 5 个新 PR（北京时间，SQLite 串行化）       │
│   开工前：实时复检 + CLA 预检，不消耗额度                          │
│   只有确认创建的新 PR 才记额度，未确认的远程写入先核实         │
│ 单条候选最多尝试 3 次（防无限重试循环）                        │
│ 额度满 → staged 暂存，次日北京时间 04:00 起 FIFO 消费                │
└──────────────────────┬───────────────────────────────────────┘
                       ▼
┌─ 复检层（认领前最后一道闸）───────────────────────────────────┐
│ issue 是否仍 open？有无 assignee？                             │
│ 有没有竞争 PR？（关键词搜索 + timeline，排除自己开的）         │
│ 有没有 claim 信号（评论里有人说"我来修"）？                    │
│ 有没有被标记 duplicate？（qdrant 教训）                        │
│ CLA 预检：需要人工签的 → needs_cla 终态，不浪费资源            │
└──────────────────────┬───────────────────────────────────────┘
                       ▼
┌─ 修复层（GitHub Actions，仓库本身）───────────────────────────┐
│ clone 目标仓库到 base SHA → opencode --auto 多轮修复          │
│   → base 上复现失败 → 补丁后通过 → 证据链 gate                 │
│   → DCO 签名（账号真身）→ push 到 fork → 开 PR                │
│   → comment_sign 类 CLA 自动签                                │
└──────────────────────┬───────────────────────────────────────┘
                       ▼
┌─ 监控层（三条通道互补）───────────────────────────────────────┐
│ Worker 秒级轮询 PR 评论 / dashboard 15 分钟 / Actions 30 分钟 │
│ 分类：fix_ci / address_review / answer_question / wait        │
│ fix_ci → 外因过滤（Vercel/CLA/等指派/flaky）→ pr-fix 修复推回 │
│ bot 机械要求（changeset）随修复单带上                          │
│ maintainer 评论 → AI 草稿落盘 → 人审后发送（绝不自动发）      │
└──────────────────────────────────────────────────────────────┘
```

## 开源贡献运行台

飞书多维表格的「开源贡献运行台」可以打开需登录的服务器页面 `/console`。页面每 5 秒读取实际队列、工作流和完成记录，按北京时间展示最近 60 分钟。新的细筛、粗筛和回复草稿事件从上线后开始累计。

「工作中」链接到已开始执行的 GitHub Actions 运行。「今日 PR 记录」来自成功创建账本，「修复记录」来自成功推送回执。「总 PR 记录」直接打开现有飞书表。

「手动触发实际消费」遵守 FIFO、暂存可消费时间与当天剩余额度。调整每日触发时间会应用于尚未到期的暂存项。模型配置应用于新任务，密钥留空时保留现值。修改 OpenCode 密钥需要服务器具备 GitHub Actions 仓库密钥管理权限。

## 目录结构

```
├─ AGENTS.md                # Playbook：全部规则与教训（必读）
├─ configs/
│  ├─ repositories.yaml     # 50 个监控仓库 + 每日额度（唯一名单真值）
│  ├─ pr_gates.yaml         # 各仓库 PR 门禁 + CLA 机制分类
│  ├─ v2.yaml               # 调度 / 路由 / 预算
│  └─ cloudflare.yaml       # Worker 配置（token 在 Secrets）
├─ scout_v2/
│  ├─ repo_policy.py        # 车道与授权（fail-closed）
│  ├─ repo_score.py         # 仓库评分模型（三指标合成）
│  ├─ state_store.py        # SQLite：额度账本 / Job / 拦截器
│  ├─ job_router.py         # scan→Modal，repair→Actions（互不兜底）
│  └─ workers/
│     ├─ auto_pipeline.py   # 认领→修复→发布 主流程
│     ├─ repair.py          # opencode 修复（repair prompt）
│     ├─ scan.py            # bug hunter 扫描（semgrep + xfail 挖掘）
│     ├─ cla.py             # CLA 三机制处理
│     ├─ pr_fix.py          # PR CI 修复（comment 触发）
│     └─ publish_remote.py  # runner 侧 push + 开 PR
├─ dashboard/               # FastAPI + HTMX 看板（127.0.0.1:8787）
├─ scripts/
│  ├─ monitor_prs.py        # PR 分类 + pr-fix 派发 + 回复草拟
│  ├─ pool_rates.py         # 池子三指标实测（改池子后必跑）
│  ├─ pool_audit.py         # 全池审视：指标 + 自动化阻碍
│  ├─ audit_ai_policy.py    # AI 政策审计（加仓库前必跑）
│  ├─ cap_audit.py          # 额度穿透审计
│  ├─ evidence_audit.py     # 证据链失败分布
│  ├─ p6_bug_hunter.py      # bug hunter（Modal 沙箱执行）
│  └─ cleanup_stale_candidates.py
├─ cloudflare/worker.js     # 发现层 Worker（每分钟）
├─ .github/workflows/
│  ├─ oss-scout-repair.yml  # 修复执行（issue → PR）
│  ├─ oss-scout-pr-fix.yml  # PR CI 修复（fork 分支推回）
│  ├─ pr-watch.yml          # PR 监控（30 分钟）
│  ├─ bh-daily.yml          # bug hunter（每日 04:00 上海）
│  ├─ worker-deploy.yml     # Cloudflare Worker 部署
│  └─ deploy.yml            # Docker 构建部署（主部署）
└─ tests_v2/ tests_dashboard/  # 343 个测试
```

## 队列与筛选可靠性

Worker 每分钟通过 GraphQL 批量扫描全部 50 仓库，分页读取，投递持久化后才推进各仓库水位。
接收请求只写 SQLite inbox；后台读取最新 issue 正文和完整评论，机械排除后调用 AI。
临时故障进入退避重试，内容变化后允许重新评估正式拒绝的 issue。

即时、轮询和手动消费共用 FIFO、执行锁和每日 5 个新 PR 的额度。超额候选保存次日
北京时间 04:00 的 eligible_at，重启后按持久时间继续；按 GitHub 确认的 PR 创建日期记账，失败不计。
新 issue 全天正常消费，包括 00:00–04:00。暂存队列从 04:00 起使用当天剩余
额度；不预留名额、不额外限速，当天 5 个名额可以在 04:00 前用完。已有 PR 的跟进只处理同一份 50 仓库名单，不占额度，回复草稿经人工确认才发送。
队列 JSON 为兼容看板保留，所有运行时读改写通过 SQLite 事务锁串行化。
飞书四张表在独立同步循环里更新，单表失败互不阻断。

## 关键设计决策（为什么长这样）

**路由互不兜底**：scan 只走 Modal（临时沙箱、ToS 合规），repair 只走 Actions
（opencode 在 Modal 瘦镜像里卡 bootstrap，实测从未成功）。不可用就 pause 上报，
绝不静默降级到跑不通的平台。

**仓库准入靠实测不看名声**：三个指标合成 0–100 分——外部合并率（45%，√ 曲线）、
中位合并耗时（35%，指数衰减）、外部 PR/周（20%，log 压缩）。低于 50 分不入池，
当前 50 个全部 ≥69 分。被否决的仓库记录在 `configs/repositories.yaml` 头部，
防止 creeping back。

**对外文字零自动化**：PR 描述只用 agent 真实产出的 root_cause；maintainer 评论
由 AI 起草但必须人审后发送。astral-sh 和 biome 的教训：AI 生成的沟通文字会直接
毁掉账号。

**DCO 用账号真身**：签名必须与 commit 作者一致，bot 逐 commit 校验。签名错了
只能重写历史或关掉重开——防不胜防，所以提交侧一步到位。

## 日常操作

```bash
# 看池子质量（改池子后必跑）
python scripts/pool_rates.py

# 全池审视（指标 + 自动化阻碍）
python scripts/pool_audit.py

# AI 政策审计（加仓库前必跑）
python scripts/audit_ai_policy.py

# 手动跑 PR 监控
python scripts/monitor_prs.py

# 手动触发 bug hunter
gh workflow run bh-daily.yml -f repos=3
```

## 部署

- **主服务**（看板 + 修复派发）：`gh workflow run deploy.yml -f target_env=aliyun`
- **Cloudflare Worker**（发现层）：`gh workflow run worker-deploy.yml`
- Worker 的 `REPOS` 改动只有重新部署 Worker 才生效

## 当前状态与已知边界

- 池子 50 个，全部实测过外部合并率 / 中位耗时 / 周机会量
- AI 政策审计 50/50 通过（零排除）
- 自动化阻碍：45 个无阻碍、3 个 comment_sign 自动签、2 个需人工签 CLA
  （cockroachdb 与 gemini-cli 已由账号持有者签过，litellm 是受限档）
- bug hunter（独立支线）：Modal 独占、每日 04:00、semgrep correctness 子集 +
  xfail 挖掘。**"findings → 开 issue"环节刻意未接**，等 correctness 命中率
  有数据再说

## 教训记录

所有踩过的坑（DCO 身份、额度穿透、run 错配、base 坏窗口、AI 政策、
duplicate issue……）连同修复方式都写在 `AGENTS.md` 里。改代码前先读它，
同样的错不犯第二次。
