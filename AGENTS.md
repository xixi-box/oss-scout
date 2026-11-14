# 当前运行约定（用户最新裁定）

以下是当前有效规则；历史案例只用于理解风险，不授权执行已删除的功能：
- 维护 50 个正式＋8 个荣誉仓库；除 ClickHouse 暂停新增 issue 扫描与修复、保留存量 PR 跟踪外，其余 57 个每分钟扫描并遵守各仓库贡献规则，荣誉身份不降低发布要求。
- Worker 只做机械排除，接收端先写 SQLite inbox，再获取正文、完整评论、评论身份与关联 PR。机械粗筛通过后才调用 OpenJev。新 issue 刚筛完就开工时复用粗筛、细筛；同日短暂等待只刷新粗筛，正文或标题变化则转核查；跨日或手动消费使用最新证据重做粗筛与细筛。
- 今天新发现的合格 issue 优先消费，有额度且执行位空闲即开工；同日等待按原入队顺序。历史暂存只在每日北京时间 04:00（可配置）形成的到期快照批次、当天 PR 关闭释放额度后补位，或人工明确消费时选取，组内按原入队时间 FIFO。普通轮询不自动消费其他历史暂存；跨日时重新等待当天批次授权。所有入口共用单个执行锁与每日额度账本。
- 候选池只展示暂存（staged）和执行中（running）。普通暂存默认滚动保留最新 10 条（以飞书「配置」生效值为准）；新 issue 粗筛、细筛通过后入队，超限按原 staged_at（缺失用 discovered_at）清除最早普通暂存，昨天、今天一视同仁，状态 queue_evicted，扫描发现显示「已清除」及原因，保留筛选结论与更新时间。不再因满队直接粗筛淘汰新增候选。不淘汰执行中、已预留派发、已派发或待对账任务。今天可消费的暂存按 FIFO 优先于新到候选；无可消费当日暂存且有额度、执行位空闲时才预留直接开工位。所有入口共用执行锁，Actions 真正开始后才标执行中。暂存 eligible_at 区分当天等待与次日 04:00，退避有效；同日短暂等待复用细筛并刷新粗筛，跨日、手动消费重新粗筛细筛。启动迁移同样按生效容量保留最新暂存，保护远程写任务。
- 每个北京时间自然日默认最多成功创建 5 个新 PR（以飞书「配置」生效值为准），以 GitHub 创建时间记账。失败、重复 PR 不占额度；结果不明的远程任务先核对再继续。
- 新 issue 全天正常发现和消费，包括 00:00–04:00；只守生效的每日 PR 总上限，不预留额度、不额外限速。
- 超额候选保存原始 staged_at 与 eligible_at；每日北京时间 04:00 为当时已到期的历史暂存生成一次持久消费快照，使用当日剩余额度逐条处理，新发现的候选仍优先。当天创建的 PR 被关闭并释放额度时，历史暂存可按原入队顺序补位，无需先达到当日上限；失败或旧日 PR 关闭不释放新额度。
- AI/取数临时失败保留并退避重试；正式否决与临时故障分开记录。
- 队列 JSON 的读改写统一使用 queue_transaction 跨线程/进程串行化；SQLite 管持久 inbox、额度与执行互斥。
- 控制器重启后中断任务转人工核查，不自动重复派发可能仍在执行的远程写操作。
- 飞书四张表独立每 15 分钟同步；完整空快照清理旧行，源失败不得冒充空结果。
- Codex 新 issue 修复使用 JSON 事件流，保存脱敏命令、输出、退出码及耗时到 Actions 诊断产物（保留 1 天），凭据目录不上传。退出和超时清理整个子进程树，包括脱离会话的构建 daemon；临时目录清理失败只记录，不覆盖原修复结果。
- OpenCode/Codex 新修复共用完整交付闸：回归测试随修复一起提交，base 仅注入测试，patched 验证完整交付；验证报告记录文件清单与补丁 SHA-256，发布前核对任务、base、结果及实际暂存补丁。安装引入的锁文件与 CI 改动须退出自动发布；构建产物不入补丁。PR 描述使用真实根因、可复跑命令、回归路径和实际工具名称。
- 发布提交统一经 commit_as_owner 创建：作者、提交者及 Signed-off-by 固定为 wangshun <96341168+xixi-box@users.noreply.github.com>，覆盖 CLI 遗留身份环境变量。历史 DCO 异常仅在用户单独授权后协助处理；优先使用仓库机器人明确支持的补签提交，不强推、不改写历史。
- 飞书「配置」包含运行设置、OpenCode、Codex、Codiv，每 30 秒读取一次，校验通过后用于新任务。「修复工具」切换 OpenCode / Codex，两套配置独立保留。Codex 使用 ChatGPT 订阅登录，登录凭据在服务器独立保存，串行借给 Actions 并回存刷新结果。按用户授权，OpenCode/Codiv 密钥在表内显示明文，公开日志和控制台查询接口不得返回密钥或 Codex 登录凭据。
- **PR 发布后由用户手动跟进**：已有 PR 的评论、评审、CI 排错、追加提交和回复不属于后台自动任务。系统只继续同步 PR 状态、飞书记录，以及当天创建的 PR 当天关闭后的额度释放。
- 已删除 PR 自动修复、评论监听与触发、回复草稿和审批卡，旧卡片操作入口已失效，飞书不再提供相关开关。不得依据历史文档、审计记录或旧任务恢复这些自动机制。
- 用户以后单独要求协助某个 PR 时，只执行该次明确授权；这不恢复后台自动跟进。


# oss-scout Playbook —— 每日自动贡献代理必读

目标：按 50 个正式＋8 个荣誉仓库名单发现合格 issue；除暂停新增修复的 ClickHouse 外，其余 57 个参与扫描与贡献。完成最小正确修复、回归测试和发布合规检查后创建新 PR，发布后的跟进交给用户。
第一约束是**账号声誉**：宁可不动作，不可发低质 claim/PR。每日 PR 额度默认 5 条，实际以飞书「配置」生效值为准。

## 0. 硬规则（违反任何一条 = 立即中止该动作并记录）

1. 每次派发只启动 **1 个合格候选**；B 级只记录不认领；C 级忽略。
2. 首次粗筛查当前开放状态、指派、认领信号和竞争 PR。刚筛完且当场拿到执行位时复用结果；等过执行位时刷新并重做机械检查，暂存消费再做一次细筛。
   按 issue 号和根因关键词搜索开放 PR。关键词命中只记录线索；GitHub 明确关联修复，或 PR 文本明确说明修复目标 issue，才按竞争拦截。
3. 永不 force-push 任何分支、永不合并上游 PR、永不跳过/篡改 CI。
4. 上游写失败（403 / `Resource not accessible by integration`）→ 记录 blocker，**本轮不重试同一写**。
5. issue 被关、标 duplicate、maintainer 明确反对 → `aborted_closed`。
6. 所有对外文字用**英文、精炼、就事论事**；不承诺交付时间；不写 AI 味长文。
7. 每个动作追加一行到 `state/worklog.jsonl`（时间、动作、对象、结果）——这是唯一审计账本。
8. **每日 PR 额度默认 5 条，以飞书「配置」生效值为准（北京时间，跨入口累计）**。SQLite publications 只统计 GitHub 确认创建的 PR，claim_days 保留尝试记录以防重复。
9. **不以年龄机械否决 issue**；是否让位给报告者由最新正文、评论和 AI 判断。以下为历史教训：龄 <2h 的新鲜 issue 曾战略放弃——那是内部人和报告者自修的主场（实测：同日快杀八成是 MEMBER 自提自修）。消费时遇到直接跳过，不计为 stale。
10. **建 issue 前必须查重。** 先按根因关键词搜该仓的 open+closed issue 和 PR（至少三组同义词），命中相似度高的一律改为**在既有 issue 下评论**，不得新开。教训：qdrant#11361 因为自己提的 issue #11360 被判 #10465 的重复而被 bot 关闭（2026-11-03）。
11. **提交 PR 前必须读完 issue 的完整评论区**，尤其 MEMBER/COLLABORATOR 的发言——维护者可能在正文之外已经表态反对某个方向。教训：mypy#22324 的修复方向被 ilevkivskyi 在 8 天前的 issue 评论里否定过，PR 提交后被直接关闭（2026-11-02）。
12. **新增回归测试必须先在 base 代码上跑一遍并确认失败。** 通过了就说明测试没覆盖到 bug，需重做。教训：deer-flow#7822 第一版测试在含 bug 的代码上照样通过，因为没走到 `positional == False` 分支（2026-11-03）。
13. **修"某处行为不对"类问题时，先 grep 出所有同类调用点**，一次性看清全貌再动手，避免只修报错那一处而漏掉兄弟路径。教训：tox#4358 只修了 package staging，漏掉 work/temp 目录和 build 路径，被更完整的 #4360 取代（2026-11-02）。

## 1.5 自动任务节奏

- Worker 每分钟扫描参与新增贡献的 57 个仓库，接收端持久入队后执行粗筛、细筛与额度检查。
- 新 issue 全天消费；历史暂存在每日北京时间 04:00 的到期快照、当天 PR 关闭释放额度或用户明确消费时参与补位，顺序遵循本文件开头约定。
- 飞书业务表每 15 分钟独立同步，配置每 30 秒读取。PR 状态与额度对账不触发已有 PR 的代码修改或回复。

## 1. 文件与状态机

```
configs/repositories.yaml  仓库名单、车道与贡献规则
configs/pr_gates.yaml      发布门禁与 CLA 规则
cloudflare/wrangler.toml   Worker 扫描名单与跟踪范围（与仓库配置保持一致）
state/seen.json        发现层去重（发现层写，他人只读）
queue/candidates.json  候选池（发现层只新增条目；消费者只改 status/lease 字段）
state/health.json      健康周检产物（>7 天先重跑 health_check.py）
state/worklog.jsonl    动作审计账本（append-only）
state/runs/YYYY-MM-DD.md  每日报告
state/feishu_webhook.txt  可选：飞书自定义机器人 URL，存在即启用通知
repos/<name>/          懒 clone 缓存（origin=xixi-box fork, --filter=blob:none）
worktrees/<repo>-<issueN>/  每个候选一个 worktree
```

候选池只展示 `staged`（暂存）和 `running`（执行中）；结束后记录 `pr_open` 或具体淘汰、失败结果。发布状态按 GitHub 同步为 open、merged 或 closed。临时取数/模型故障退避重试，正式否决不作为临时故障重派。JSON 写入使用 queue_transaction 与原子替换。

## 2. 新 PR 自动流程与发布边界

### PR 发布后的状态同步

只读取和同步 PR 的 open、merged、closed 状态，更新飞书记录、扫描发现及每日额度账本。仅当天创建的 PR 当天关闭可释放当天额度；旧日 PR 关闭和合并不返还额度。状态变化不触发修复、评论、草稿或审批操作。

用户负责发布后的评审沟通与 CI 处理。不得定时拉取 PR 评论、分类评审、派发后续修复、自动补充说明、催评审或发送回复。

### 发现与筛选
生产发现入口是每分钟 Worker 扫描与 SQLite inbox。完整取数后做机械粗筛，再交给 OpenJev 细筛。
新候选为 0 → 简短收尾（这是常态空跑，不报错）。

### 新 issue 修复与发布
默认每日最多成功创建 5 个新 PR；以飞书「配置」生效值为准。今天可消费的暂存按原入队时间优先；没有可消费当日暂存时，新到的合格候选可直接开工。历史补位只使用已授权的消费批次。

**暂存机制（2026-10-30 起）**：当天额度用完之后又识别出可处理的 issue，标
`staged` 记时间，可消费时间顺延到次日 04:00，是否被消费仍遵循现行批次授权、当日优先及额度规则。
暂存超过 **3 天**丢弃——放三天的 issue 多半已经被人修了或被关了。
`staged_at` 一旦写下就不再刷新，否则永远等不到过期。
流程：
1. 复检（§0 第 2 条）。
2. `gh repo sync xixi-box/<repo>` 同步 fork。
3. 懒 clone：`repos/` 无此仓库才 `git clone --filter=blob:none git@github.com:xixi-box/<repo>.git`（或 https）；
   已有则 pull。**不顺手更新无关仓库。**
4. `git worktree add worktrees/<repo>-<issueN> -b fix/<issueN>-<短slug>`。
5. 最小正确修复：改动通常 ≤5 文件，不顺手重构、不动无关格式。回归测试照该仓库现有测试风格写，
   跑相关子集（Python 通常 `uv run pytest tests/<相关文件> -x`；TS 看 package.json）。环境跑不起来 →
   如实记录失败原因和日志，停止自动发布；不得用未验证的 Draft PR 绕过交付验证。
6. 过合规门（§4）→ 开 PR（`Fixes #N`；标题遵循仓库约定；`maintainer_can_modify=true`；
   改动+测试完整才 ready-for-review）。
7. 更新候选状态、worklog、每日报告；有 A 级认领/新 PR/blocker 时发飞书（若启用）。

**闸门池专属**：不直接开 PR。发分析评论（模板见 §5）→ status=`awaiting_assignment`。
此后每天消费段开头查一次 assignees：指派给我 → 走实现；指派给他人 → `competed`。
**绝不发 "please assign me" 类评论**（MCP 文档明文禁止，各闸门仓库同样反感）。

**限制档（litellm）**：只消费 discover 标了 `maintainer_confirm` 或带 `help wanted` 的候选。

## 3. A/B/C 判定（discover 给粗分，最终判断在这里做）

升级为 A 的额外条件：根因能定位到具体模块、回归测试可行、预期 ≤5 文件、无 API 重设计。
任何一条不满足但值得做 → B（只通知）。涉及大重构/依赖升级/协议变更 → C（忽略）。
`health.json` 的 `lane_advisory` 与 config 冲突时**以 health 为准**（仓库健康变了）。

## 3.5 加仓库前的准入闸（2026-10-30 补）

合并率高不代表能进池。加任何仓库前先跑 `python scripts/audit_ai_policy.py`，
再人读一遍它报出来的原文。硬性排除：

- **禁止自主 agent 贡献**（astral-sh 的 AI_POLICY.md 就是），确认会被执行。
- **结构性 CI 门槛**：要求作者先有 N 个已合并 PR 才允许跑 CI（vllm 的
  `pre-run-check` 要 4 个），新号永远满足不了，开 PR 必然挂红。
- **未指派即自动关 PR** 且没有"先开 PR 等指派"的明文豁免（langchain）。

需人读但不一定排除：只禁止 AI 写**沟通文字**、允许 AI 写代码的仓库（biome、
weaviate）——可以进池，但描述必须自己写得像人话。

## 4. PR 合规门（每个 PR 开出前逐项过，一条不过就不开）

1. **重读**该仓库 CONTRIBUTING.md + PR 模板（`.github/PULL_REQUEST_TEMPLATE.md`）。
2. 看**最近 3 个被合并的外部 PR**的标题格式、描述结构、测试形态，照着做。
3. 查 CLA/DCO。**DCO**：要求时每个 commit 都要有 `Signed-off-by`（发布代码已
   无条件加）。发布前核对全部提交的作者、提交者与签名。已发布 PR 的 DCO 异常交给用户处理；仅在用户单独授权后协助，不自动补签、改写历史或关闭重开。
   **CLA** 分三种（`configs/pr_gates.yaml` 的 `cla:` 段已核实）：
   - `comment_sign`（phoenix）：回一条固定口令即完成，**代码会自动签**。
   - `cla-assistant`（mem0 / litellm / langfuse）：点链接走 OAuth，**必须人工**。
     这种仓库开 PR 前会被 preflight 拦下并提示先去签，不会白开一个挂着 not_signed 的 PR。
   - `microsoft` EasyCLA（playwright ×2）：同样人工。
4. 查**结构性门槛**：有的仓库要求作者先有 N 个已合并 PR 才允许跑 CI（vllm 的
   `pre-run-check` 要求 4 个，并明写不要 AI agent 申请标签）。新账号不满足，
   这类仓库在挑选阶段就该排除，不要开 PR。
5. 描述结构：what → root cause → how → tests → compatibility → 未验证项。精炼。
6. 分支命名 `fix/<issueN>-<slug>`；提交信息遵循仓库约定。
7. 不 @ 打扰 maintainer，不在 PR 里说"我很快会…"这类承诺。
8. **改之前先跑那个包已有的测试**。2026-10-31 实录（milvus #54924）：我们把
   `PresizeAppendFieldData` 的 `rows <= 0` 分支改成返回 typed 空列，结果撞上
   仓库里已有的 `TestPresizeAppendFieldDataColumns/no_rows_keeps_every_column_nil`
   ——那个测试明确要求返回 nil，而且该语义还被 proxy 依赖。CI 只报 `UT-GO`
   失败，看不出是断言冲突；在本地跑一遍 `go test ./<pkg>/` 十秒就能发现。
   **改任何行为之前，先跑那个包的全量测试，而不是只跑自己新写的用例。**
9. **先看 issue 有没有被上游分析过**。同一个 issue 里，milvus 的 team2 机器人
   给出了比我们更准的根因（修复点应该在 `SearchCommonReduce`，不是
   `PresizeAppendFieldData`）。认领前翻一遍 issue 的全部评论——有人已经定位过
   就别重复劳动，照着那个方向做或者放弃。

## 5. 认领评论模板（英文，可按 issue 内容增删，保持 ≤6 行）

> **2026-10-30 教训**：PR 描述的 `Root cause:` 曾由模板拿 issue 标题填充，同一句话
> 出现两遍，读起来像假装做了归因。biome / astral 这类仓库会据此判定"AI 生成沟通"
> 并直接关 PR。现在描述里的根因只来自 agent 真实产出（`result.json` 的
> `root_cause`），没有就整段不写。

开放池：
```text
Hi! I'd like to work on this. The failure looks like it originates in <模块/函数边界>.
I plan to <一句话修复思路> and add a regression test covering <复现场景>.
Does that approach sound right?
```

闸门池（对齐方案、等指派，不写任何求指派字眼）：
```text
I reproduced this on <版本/环境>. <两行以内根因分析>.
I'd propose fixing it by <思路>, with a regression test for <场景>.
Happy to prepare a PR if that approach works for you.
```

## 6. 仓库特殊规则（config.json 同步维护，动手前再核对 CONTRIBUTING 最新版）

| 仓库 | 规则 |
|---|---|
| deepset-ai/haystack | PR 标题用 conventional commits；若 PR 全部由 AI 生成，描述里加一句简短 AI 声明 |
| anomalyco/opencode | 默认分支 dev；PR 描述必须精炼，禁止 AI 长文（会被忽略）；issue 必须严格按模板填，否则 2 小时后被自动关；优先 help-wanted 标签 issue |
| pydantic/pydantic-ai | **未指派 PR 可能被自动关**；评论对齐 → 等指派 → 指派后才开 PR |
| microsoft/playwright | issue 被 triage 且 approved 后才能提 PR；留意 CLA |
| microsoft/agent-framework | 可在 issue 里请求指派；只碰 Python 侧，.NET 不在范围 |
| BerriAI/litellm | 限制档；PR 必须带测试，按其 CONTRIBUTING checklist 逐项过 |
| OpenHands/OpenHands、OpenHands/software-agent-sdk、infiniflow/ragflow | 无 CONTRIBUTING 文件，首次动手前人工读最近合并的外部 PR 摸规范 |
| langfuse | 本地测试环境重（TS+ClickHouse），验证无法完成时停止自动发布并记录原因 |
| 其余开放池 | 按 §4 通用合规门 |

## 7. 报告

每日写 `state/runs/YYYY-MM-DD.md`：发现、筛选、修复与发布结果、新候选清单、认领/PR 动作、blocker、健康分异动。
最终 chat 回复给用户一段简明结论（做了什么/发现了什么/什么被挡住了）。
有新鲜 A 级候选或 blocker 时，若 `state/feishu_webhook.txt` 存在则同步飞书。
