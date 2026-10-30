"""合并贡献度与门禁数据，输出最终可提交仓库名单。"""
import json
from pathlib import Path

fr = {r["repo"]: r for r in json.loads(Path("state/agent_friendliness.json").read_text(encoding="utf-8"))}
au = {r["repo"]: r for r in json.loads(Path("state/agent_pool_audit.json").read_text(encoding="utf-8")) if r.get("ok")}

GATED = {
    "mem0ai/mem0": "pr-gate.yml 关闭未事先同意承接的 PR",
    "pydantic/pydantic-ai": "pr-gates.yml 关闭未指派 PR",
    "langchain-ai/deepagents": "require_issue_link 会关闭未指派 PR",
    "langchain-ai/langchain": "require-issue-link bot",
    "langchain-ai/langgraph": "require_issue_link.yml",
    "pydantic/pydantic": "enforce-issue-assignment.yml",
    "sqlalchemy/sqlalchemy": "sqla-tester 需要 invite label",
}
AUTO_OK = {"Python", "Go", "TypeScript", "JavaScript", "Rust"}

rows = []
for repo, f in fr.items():
    a = au.get(repo) or {}
    if not f.get("ext") or f["ext"] < 8:
        continue
    rows.append({
        "repo": repo,
        "lang": a.get("lang") or "?",
        "stars": a.get("stars", 0),
        "rate": f["ext_merge_rate"],
        "ext": f["ext"],
        "merged": f["ext_merged"],
        "pushed": (a.get("pushed") or "")[:10],
        "gated": repo in GATED,
        "auto_ok": (a.get("lang") in AUTO_OK),
    })

Path("state/agent_final.json").write_text(
    json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")

good = [r for r in rows if not r["gated"] and r["auto_ok"] and r["rate"] >= 0.5]
good.sort(key=lambda x: (-x["rate"], -x["stars"]))
print("总测 %d | 合格 %d\n" % (len(rows), len(good)))
for r in good:
    print("{:4.0%} {:10s} {:>7,} {} {}".format(
        r["rate"], r["lang"], r["stars"], r["pushed"], r["repo"]))
