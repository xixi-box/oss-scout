# oss-scout Playbook —— 每日自动贡献代理必读

目标：在 16+1 个监控仓库里发现合格 issue → 认领 → 最小正确修复 + 回归测试 → 按各仓库规范开 PR。
第一约束是**账号声誉**：宁可不动作，不可发低质 claim/PR。每天至多 1 个新认领。

## 0. 硬规则（违反任何一条 = 立即中止该动作并记录）

1. 每次运行最多**认领/实现 1 个新 A 级候选**；B 级只记录不认领；C 级忽略。
2. 认领前**实时复检**（不许用队列快照）：issue 是否仍 open、assignees 是否为空（闸门池是否指派给我）、
   新评论里有无认领信号、timeline 有无新关联 PR、按 issue 号+根因关键词搜 open PR。任一命中竞争 → `aborted_competed`，换下一条。
3. 永不 force-push 任何分支、永不合并上游 PR、永不跳过/篡改 CI。
4. 上游写失败（403 / `Resource not accessible by integration`）→ 记录 blocker，**本轮不重试同一写**。
5. issue 被关、标 duplicate、maintainer 明确反对 → `aborted_closed`。
6. 所有对外文字用**英文、精炼、就事论事**；不承诺交付时间；不写 AI 味长文。
7. 每个动作追加一行到 `state/worklog.jsonl`（时间、动作、对象、结果）——这是唯一审计账本。
8. **每日至多 1 个新认领（跨窗口累计）**：开工先查 `state/worklog.jsonl` 当日是否已有 claim/awaiting_assignment 动作，有则本轮只做监控与发现。
9. **龄 <2h 的新鲜 issue 战略放弃**——那是内部人和报告者自修的主场（实测：同日快杀八成是 MEMBER 自提自修）。消费时遇到直接跳过，不计为 stale。

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

状态机：`new → awaiting_assignment（仅闸门池）→ assigned_to_me → claimed → pr_open → merged | closed | aborted_* | escalated`
实现失败退回 `new`，同一候选失败 2 次 → `escalated` 并在报告里说明。写 JSON 一律临时文件 + 原子替换。

## 2. 每日三段（每段独立提前退出）

### ① PR 监控段（最优先，几秒钟）
`python scripts/monitor_prs.py` 扫所有 open PR，按 CI 结果和新评论分类：
`fix_ci` / `address_review` / `answer_question` / `wait`。
- CI 因我们的改动失败 → 读日志、修复、推送（新 commit，不 force-push）。
- **先排除 flaky**：连真实 LLM 的测试（如 pydantic-ai 的 `test_exploration[gpt-live]`）、
  Vercel 授权类检查，失败与我们无关，不要当成自己的 bug 去改。
- maintainer 提修改意见 → 本轮优先落实；**48 小时内必须响应**（防烂尾第一杠杆）。
- 被合并/被关 → 更新状态，被关要看原因记入 worklog（影响该仓库后续出价意愿）。
  **被关的 PR 要回溯到仓库规则**：2026-10-30 一天被关 3 个（astral AI 政策、
  langchain 未指派、biome 低信号描述），全不是代码问题。任何一个被关就去读
  那个仓库的 CONTRIBUTING/AI_POLICY，把规则补进 `configs/` —— 同样的错不犯第二次。
- 有新 PR 开出 ≥2 天无任何 review 且该仓库健康分高 → 可在 PR 里礼貌补充测试/说明，不催促。

### ② 发现段
`python discover.py`（全量 ~5 分钟）。输出摘要里 A/B 级候选入队即止，本段不做实现。
新候选为 0 → 简短收尾（这是常态空跑，不报错）。

### ③ 消费段（每日至多 2 个）
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
