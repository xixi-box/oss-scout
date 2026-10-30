"""生成最终可提交仓库名单文档。"""
import json
import subprocess
from pathlib import Path

rows = json.loads(Path("state/agent_final.json").read_text(encoding="utf-8"))
good = [r for r in rows if not r["gated"] and r["auto_ok"] and r["rate"] >= 0.5]

# 剔除：停更 / 样本过小 / 超大单体仓
EXCLUDE = {
    "llm-tools/embedJs": "2026-06 后停更",
    "awslabs/agent-evaluation": "2025-12 后停更",
    "Significant-Gravitas/AutoGPT": "超大单体仓库，issue 多在深水区",
}
FINAL = [r for r in good if r["repo"] not in EXCLUDE]
FINAL.sort(key=lambda x: (-x["rate"], -x["stars"]))

CATEGORY = {
    # Agent Harness / Framework
    "openai/openai-agents-python": ("Agent 框架", "OpenAI 官方轻量多 agent 框架，Python 类型友好"),
    "bytedance/deer-flow": ("Agent Harness", "长时程 SuperAgent，研究向"),
    "VoltAgent/voltagent": ("Agent 框架", "TypeScript 版 agent 工程平台"),
    # Memory
    "vectorize-io/hindsight": ("Agent Memory", "主打自学习记忆"),
    "tigerless-labs/agent-memory": ("Agent Memory", "纯 Markdown 存记忆，无数据库依赖"),
    "oceanbase/powercontext": ("Agent Memory", "长期记忆 + 跨 agent 交接"),
    "volcengine/OpenViking": ("Agent Memory", "自演化上下文数据库"),
    "ruvnet/RuVector": ("Agent Memory", "高性能实时决策向量层"),
    # RAG
    "HKUDS/RAG-Anything": ("RAG", "一站式 RAG 框架"),
    "OSU-NLP-Group/HippoRAG": ("RAG", "NeurIPS'24，类海马体检索思路"),
    # Vector DB
    "lancedb/lancedb": ("向量库", "嵌入式列式向量库，无服务端"),
    "milvus-io/milvus": ("向量库", "云原生，亿级规模"),
    "weaviate/weaviate": ("向量库", "自带混合检索（向量+BM25）"),
    "qdrant/qdrant": ("向量库", "Rust，单机首选，过滤强"),
    # MCP
    "PrefectHQ/fastmcp": ("MCP", "写 MCP server 的首选库"),
    "modelcontextprotocol/registry": ("MCP", "官方 server 注册表"),
    "upstash/context7": ("MCP", "给 agent 提供最新库文档"),
    # Eval / Observability
    "Arize-ai/phoenix": ("可观测/评估", "trace + eval 一体，agent 轨迹可视"),
    "confident-ai/deepeval": ("评估", "pytest 风格 agent 单测"),
    "promptfoo/promptfoo": ("评估", "prompt 测试与红队"),
    "langfuse/langfuse": ("可观测", "LLM 应用追踪，开源自托管"),
    # Sandbox / Runtime
    "firecracker-microvm/firecracker": ("沙箱", "AWS 的 microVM，跑不可信代码的事实标准"),
    # Tooling
    "pypa/virtualenv": ("工程工具", "Python 虚拟环境标准实现"),
    "pypa/pip-tools": ("工程工具", "依赖锁定"),
    "tox-dev/tox": ("工程工具", "测试环境矩阵"),
    "psf/black": ("工程工具", "代码格式化"),
    "biomejs/biome": ("工程工具", "Rust 写的 JS/TS 工具链"),
    "astral-sh/uv": ("工程工具", "极快的 Python 包管理器"),
    "astral-sh/ruff": ("工程工具", "Rust 写的 Python linter"),
    # Model serving
    "vllm-project/vllm": ("推理服务", "高吞吐 LLM 推理"),
    # Realtime / Collab
    "instantdb/instant": ("实时数据", "前端直连数据库"),
    "liveblocks/liveblocks": ("实时协作", "协作基础设施"),
    "yjs/yjs": ("实时协作", "CRDT 事实标准"),
    "dxos/dxos": ("去中心化", "本地优先的协作运行时"),
    "tldraw/tldraw": ("画布", "无限画布 SDK"),
    # Data
    "supabase/supabase": ("后端平台", "Postgres + 全家桶"),
    # Dev experience
    "zuplo/zudoku": ("文档", "OpenAPI 驱动 API 文档"),
    # Misc
    "microsoft/playwright-python": ("浏览器自动化", "Playwright 的 Python 绑定"),
    "microsoft/playwright": ("浏览器自动化", "跨浏览器自动化框架"),
    "BerriAI/litellm": ("LLM 网关", "统一 100+ 模型接口"),
}


def desc(repo):
    p = subprocess.run(["gh", "api", "repos/" + repo, "--jq", ".description"],
                       capture_output=True, text=True, timeout=60)
    return (p.stdout or "").strip() or "—"


lines = [
    "# 最终可提交仓库名单",
    "",
    "生成时间 2026-10-30。测量方法：取每个仓库最近 60 个已关闭 PR，统计",
    "**非成员提交且被合并的比例**（外部贡献者 = `author_association` 属于",
    "`NONE` / `CONTRIBUTOR` / `FIRST_TIME_CONTRIBUTOR`）。",
    "",
    "筛选条件：合并率 ≥50%、语言可自动验证（Python/Go/TS/JS/Rust）、",
    "无会关闭未受邀 PR 的 bot、非停更、样本量足够。",
    "",
    "共 **%d** 个。" % len(FINAL),
    "",
    "---",
    "",
    "## 按类别",
    "",
]

by_cat = {}
for r in FINAL:
    cat, note = CATEGORY.get(r["repo"], ("其他", ""))
    by_cat.setdefault(cat, []).append((r, note))

for cat in sorted(by_cat, key=lambda c: -len(by_cat[c])):
    lines += ["### " + cat, "", "| 仓库 | 合并率 | stars | 说明 |", "|---|---|---|---|"]
    for r, note in sorted(by_cat[cat], key=lambda x: -x[0]["rate"]):
        lines.append("| `%s` | %d%% | %s | %s |" % (
            r["repo"], round(r["rate"] * 100), format(r["stars"], ","), note or desc(r["repo"])))
    lines.append("")

lines += [
    "---",
    "",
    "## 完整排序（按合并率）",
    "",
    "| # | 仓库 | 合并率 | 外部样本 | 语言 | stars | 最近提交 |",
    "|---|---|---|---|---|---|---|",
]
for i, r in enumerate(FINAL, 1):
    lines.append("| %d | `%s` | %d%% | %d/%d | %s | %s | %s |" % (
        i, r["repo"], round(r["rate"] * 100), r["merged"], r["ext"],
        r["lang"], format(r["stars"], ","), r["pushed"]))

lines += [
    "",
    "---",
    "",
    "## 剔除记录",
    "",
    "测量过但不适合提交的，附原因。",
    "",
    "### 会关闭未受邀 PR（硬门禁）",
    "",
    "| 仓库 | 原因 |",
    "|---|---|",
    "| `mem0ai/mem0` | `pr-gate.yml` 明确关闭未事先同意承接的 PR |",
    "| `pydantic/pydantic-ai` | `pr-gates.yml` 关闭未指派 PR |",
    "| `langchain-ai/deepagents` | `require_issue_link` 会关闭未指派 PR |",
    "| `langchain-ai/langchain` | `require-issue-link` bot |",
    "| `langchain-ai/langgraph` | `require_issue_link.yml` |",
    "| `pydantic/pydantic` | `enforce-issue-assignment.yml` |",
    "| `sqlalchemy/sqlalchemy` | `sqla-tester` 需要 issue 带 invite label |",
    "",
    "### 停更 / 样本不足 / 超大单体",
    "",
    "| 仓库 | 原因 |",
    "|---|---|",
]
for repo, why in EXCLUDE.items():
    lines.append("| `%s` | %s |" % (repo, why))

lines += [
    "",
    "### 合并率过低（<50%）",
    "",
    "测过但外部 PR 大多合不进去，列几个代表：",
    "",
]

low = [r for r in rows if not r["gated"] and r["rate"] < 0.5 and r["ext"] >= 15]
low.sort(key=lambda x: -x["rate"])
for r in low[:14]:
    lines.append("- `%s` — %d%%（%d/%d）" % (
        r["repo"], round(r["rate"] * 100), r["merged"], r["ext"]))

lines += [
    "",
    "---",
    "",
    "## 说明",
    "",
    "**合并率是快照，会变。** 建议每隔一两个月重跑一次：",
    "",
    "```bash",
    "python scripts/audit_contrib_friendliness.py --pool <名单> --out <输出>",
    "python scripts/merge_pool_results.py",
    "```",
    "",
    "**合并率高不等于容易修。** 它只说明「这个仓库愿意收外人的 PR」，",
    "具体某个 issue 好不好修，还要看 issue 本身的描述质量。",
    "",
    "**星数只作规模参考。** 一个 5k star 但合并率 90% 的仓库，比 90k star",
    "但合并率 30% 的仓库更容易出成果。",
]

Path("docs/final_repos.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
print("wrote docs/final_repos.md with", len(FINAL), "repos")
