# 当前运行约定（用户最新裁定）

以下约定覆盖后文历史节奏与额度描述：
- 保留现有 50 个外部监控仓库，每分钟扫描；名单不变。
- Worker 只做机械排除，接收端先写 SQLite inbox，再获取正文、完整评论、评论身份与关联 PR。机械粗筛通过后才调用 OpenJev。新 issue 即时派发只复检 GitHub 事实，正文或标题变化则转核查。暂存队列到期消费和手动消费复用同一套粗筛与细筛，使用当时的证据。
- 合格候选统一进入 FIFO；所有自动/手动入口共用单个执行锁与每日额度账本。
- 每个北京时间自然日最多成功创建 5 个新 PR，以 GitHub 创建时间记账。失败、重复 PR 和已有 PR 的跟进不占额度；结果不明的远程任务先核对再继续。
- 新 issue 全天正常发现和消费，包括 00:00–04:00；只守每日 5 条总上限，不预留额度、不额外限速。
- 超额候选保存原始 staged_at 与 eligible_at，次日北京时间 04:00 起按时间消费，使用当日剩余额度；额度已用完就继续排队。
- AI/取数临时失败保留并退避重试；正式否决与临时故障分开记录。
- 队列 JSON 的读改写统一使用 queue_transaction 跨线程/进程串行化；SQLite 管持久 inbox、额度与执行互斥。
- 控制器重启后中断任务转人工核查，不自动重复派发可能仍在执行的远程写操作。
- 飞书四张表独立每 15 分钟同步；完整空快照清理旧行，源失败不得冒充空结果。

# oss-scout Playbook —— 每日自动贡献代理必读

目标：在 16+1 个监控仓库里发现合格 issue → 认领 → 最小正确修复 + 回归测试 → 按各仓库规范开 PR。
第一约束是**账号声誉**：宁可不动作，不可发低质 claim/PR。每个北京时间自然日最多成功创建 5 个新 PR。

## 0. 硬规则（违反任何一条 = 立即中止该动作并记录）

1. 每次派发只启动 **1 个合格候选**；B 级只记录不认领；C 级忽略。
2. 首次粗筛查当前开放状态、指派、认领信号和竞争 PR。刚筛完且当场拿到执行位时复用结果；等过执行位时刷新并重做机械检查，暂存消费再做一次细筛。
   按 issue 号和根因关键词搜索开放 PR。关键词命中只记录线索；GitHub 明确关联修复，或 PR 文本明确说明修复目标 issue，才按竞争拦截。
3. 永不 force-push 任何分支、永不合并上游 PR、永不跳过/篡改 CI。
4. 上游写失败（403 / `Resource not accessible by integration`）→ 记录 blocker，**本轮不重试同一写**。
5. issue 被关、标 duplicate、maintainer 明确反对 → `aborted_closed`。
6. 所有对外文字用**英文、精炼、就事论事**；不承诺交付时间；不写 AI 味长文。
7. 每个动作追加一行到 `state/worklog.jsonl`（时间、动作、对象、结果）——这是唯一审计账本。
8. **每天最多成功创建 5 个新 PR（北京时间，跨入口累计）**。SQLite publications 只统计 GitHub 确认创建的 PR，claim_days 保留尝试记录以防重复。
9. **不以年龄机械否决 issue**；是否让位给报告者由最新正文、评论和 AI 判断。以下为历史教训：龄 <2h 的新鲜 issue 曾战略放弃——那是内部人和报告者自修的主场（实测：同日快杀八成是 MEMBER 自提自修）。消费时遇到直接跳过，不计为 stale。
10. **建 issue 前必须查重。** 先按根因关键词搜该仓的 open+closed issue 和 PR（至少三组同义词），命中相似度高的一律改为**在既有 issue 下评论**，不得新开。教训：qdrant#11361 因为自己提的 issue #11360 被判 #10465 的重复而被 bot 关闭（2026-11-03）。
11. **提交 PR 前必须读完 issue 的完整评论区**，尤其 MEMBER/COLLABORATOR 的发言——维护者可能在正文之外已经表态反对某个方向。教训：mypy#22324 的修复方向被 ilevkivskyi 在 8 天前的 issue 评论里否定过，PR 提交后被直接关闭（2026-11-02）。
12. **新增回归测试必须先在 base 代码上跑一遍并确认失败。** 通过了就说明测试没覆盖到 bug，需重做。教训：deer-flow#7822 第一版测试在含 bug 的代码上照样通过，因为没走到 `positional == False` 分支（2026-11-03）。
13. **修"某处行为不对"类问题时，先 grep 出所有同类调用点**，一次性看清全貌再动手，避免只修报错那一处而漏掉兄弟路径。教训：tox#4358 只修了 package staging，漏掉 work/temp 目录和 build 路径，被更完整的 #4360 取代（2026-11-02）。

## 1.5 两类定时任务

- **观测任务**（每 2 小时）：只跑 `python discover.py`，结果进队列+飞书。禁止消费/评论/开 PR。
- **消费任务**（每日 10:30/16:30/22:30 三窗）：完整三段。两任务共享第 8 条的每日认领上限。

## 1. 文件与状态机

```
config.json            监控名单+车道+仓库特殊规则（改名单只改这里）
state/seen.json        发现层去重（发现层写，他人只读）
queue/candidates.json  候选池（发现层只新增条目；消费者只改 status/lease 字段）
state/health.json      健康周检产物（>7 天先重跑 health_check.py）
state/worklog.jsonl    动作审计账本（append-only）
state/runs/YYYY-MM-DD.md  每日报告
state/feishu_webhook.txt  可选：飞书自定义机器人 URL，存在即启用通知
repos/<name>/          懒 clone 缓存（origin=xixi-box fork, --filter=blob:none）
worktrees/<repo>-<issueN>/  每个候选一个 worktree
```

状态机：`new → staged → awaiting_assignment（仅闸门池）→ assigned_to_me → claimed → pr_open → merged | closed | aborted_* | escalated`
实现失败退回 `new`，同一候选失败 2 次 → `escalated` 并在报告里说明。写 JSON 一律临时文件 + 原子替换。

PR 跟进只处理同一份 50 仓库名单，改代码与草稿均不占新 PR 额度。回复由人工确认发送。

## 2. 每日三段（每段独立提前退出）

### ① PR 监控段（最优先，几秒钟）
两条通道，互为备份：
- **Worker 轮询**（每分钟）：Cloudflare Worker 按 KV 里的 `prs` 列表拉
  `comments?since=`，命中就 POST `/api/ingest/pr-comments` → 有新人说话几十秒内
  就能知道。**这是"评论即时响应"的主通道。**
- **Actions 定时**（每 30 分钟）+ dashboard 循环（每 15 分钟）：`monitor_prs.py`
  全量分类，同时把 PR 列表同步回 KV 供 Worker 使用。

分类：`fix_ci` / `address_review` / `answer_question` / `wait`。
- CI 因我们的改动失败 → 读日志、修复、推送（新 commit，不 force-push）。
- **先排除 flaky**：连真实 LLM 的测试（如 pydantic-ai 的 `test_exploration[gpt-live]`）、
  Vercel 授权类检查，失败与我们无关，不要当成自己的 bug 去改。
- 需要 maintainer 加标签的检查（pydantic-ai 的 `Package Label`）也是等，不是修。
- maintainer 提修改意见 → 本轮优先落实；**48 小时内必须响应**（防烂尾第一杠杆）。
- **不自动发评论**：维护者提了意见该怎么回需要人判断（astral-sh 明文禁止 AI 写
  沟通，biome 说低信号描述会被直接关）。系统只负责尽快让你知道，回复由人写。
- 被合并/被关 → 更新状态，被关要看原因记入 worklog（影响该仓库后续出价意愿）。
  **被关的 PR 要回溯到仓库规则**：2026-10-30 一天被关 3 个（astral AI 政策、
  langchain 未指派、biome 低信号描述），全不是代码问题。任何一个被关就去读
  那个仓库的 CONTRIBUTING/AI_POLICY，把规则补进 `configs/` —— 同样的错不犯第二次。
- 有新 PR 开出 ≥2 天无任何 review 且该仓库健康分高 → 可在 PR 里礼貌补充测试/说明，不催促。

### ② 发现段
`python discover.py`（全量 ~5 分钟）。输出摘要里 A/B 级候选入队即止，本段不做实现。
新候选为 0 → 简短收尾（这是常态空跑，不报错）。

### ③ 消费段（每日最多成功创建 5 个新 PR）
优先级：**暂存 > 开放池 new A > 闸门池 assigned_to_me > 限制档 maintainer-confirmed**。

**暂存机制（2026-10-30 起）**：当天额度用完之后又识别出可处理的 issue，标
`staged` 记时间，**留到第二天优先做**。drain 的顺序是"暂存优先、组内先到先出"。
暂存超过 **3 天**丢弃——放三天的 issue 多半已经被人修了或被关了。
`staged_at` 一旦写下就不再刷新，否则永远等不到过期。
流程：
1. 复检（§0.2）。
2. `gh repo sync xixi-box/<repo>` 同步 fork。
3. 懒 clone：`repos/` 无此仓库才 `git clone --filter=blob:none git@github.com:xixi-box/<repo>.git`（或 https）；
   已有则 pull。**不顺手更新无关仓库。**
4. `git worktree add worktrees/<repo>-<issueN> -b fix/<issueN>-<短slug>`。
5. 最小正确修复：改动通常 ≤5 文件，不顺手重构、不动无关格式。回归测试照该仓库现有测试风格写，
   跑相关子集（Python 通常 `uv run pytest tests/<相关文件> -x`；TS 看 package.json）。环境跑不起来 →
   如实说明，开 **Draft PR** 并在描述里写明未验证项，绝谎称测试通过。
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
   无条件加）。注意 DCO bot 校验分支上的**全部** commit：事后追加一个签名空 commit
   不解除红灯，唯一修法是 rebase 成单个签名 commit——那需要 force-push，违反 §0.3，
   所以只能关掉重开。
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
| langfuse | 本地测试环境重（TS+ClickHouse），跑不动就 Draft+如实说明 |
| 其余开放池 | 按 §4 通用合规门 |

## 7. 报告

每日写 `state/runs/YYYY-MM-DD.md`：三段结果、新候选清单、认领/PR 动作、blocker、健康分异动。
最终 chat 回复给用户一段简明结论（做了什么/发现了什么/什么被挡住了）。
有新鲜 A 级候选或 blocker 时，若 `state/feishu_webhook.txt` 存在则同步飞书。
