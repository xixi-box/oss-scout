"""核查扫描池里每个仓库是否适合作为自动贡献目标。

    python scripts/audit_pool.py [--pool FILE] [--out FILE]

只看客观事实，不做主观评级：
- 能不能访问（重定向到哪、是否 404）
- issues 是否开启（关了就没法从 issue 驱动）
- 是否 archived（不再接受贡献）
- 主语言
- 有没有会在 PR 上自动关闭的 bot（查 .github/workflows 里的关键词）
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 这些关键词出现在 workflow 名/内容里，通常意味着"未指派/未受邀的 PR 会被处理"
GATE_HINTS = ("require-issue-link", "missing-issue-link", "needs-assignee",
              "assign", "triage", "auto-close", "autoclose", "pr-gate",
              "first-time", "welcome")


def gh(args: list[str], timeout: int = 60) -> tuple[int, str]:
    p = subprocess.run(["gh"] + args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    return p.returncode, (p.stdout or "")


def audit(repo: str) -> dict:
    rc, out = gh(["api", f"repos/{repo}",
                  "--jq", "{full:.full_name,issues:.has_issues,archived:.archived,"
                          "lang:.language,stars:.stargazers_count,fork:.fork,"
                          "pushed:.pushed_at}"])
    if rc != 0:
        return {"repo": repo, "ok": False, "why": "not found / no access"}
    try:
        info = json.loads(out)
    except json.JSONDecodeError:
        return {"repo": repo, "ok": False, "why": "unparsable"}

    rec = {
        "repo": info.get("full") or repo,
        "requested": repo,
        "issues": bool(info.get("issues")),
        "archived": bool(info.get("archived")),
        "lang": info.get("lang") or "",
        "stars": info.get("stars") or 0,
        "pushed": (info.get("pushed") or "")[:10],
        "redirected": (info.get("full") or repo) != repo,
    }
    rec["ok"] = rec["issues"] and not rec["archived"]

    # 扫 workflow 文件名找门禁迹象
    rc2, out2 = gh(["api", f"repos/{rec['repo']}/contents/.github/workflows",
                    "--jq", "[.[].name]"], timeout=60)
    gates: list[str] = []
    if rc2 == 0:
        try:
            names = json.loads(out2 or "[]")
        except json.JSONDecodeError:
            names = []
        for n in names:
            low = n.lower()
            if any(h in low for h in GATE_HINTS):
                gates.append(n)
    rec["gate_workflows"] = gates[:6]
    rec["gate_hint"] = bool(gates)
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", default=str(ROOT / "configs" / "scan_pool_wide.txt"))
    ap.add_argument("--out", default=str(ROOT / "state" / "pool_audit.json"))
    args = ap.parse_args()

    repos: list[str] = []
    for line in Path(args.pool).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            repos.append(line)

    results = []
    for i, r in enumerate(repos, 1):
        rec = audit(r)
        results.append(rec)
        flag = "OK " if rec.get("ok") else "XX "
        gate = " GATE?" if rec.get("gate_hint") else ""
        redir = f" -> {rec['repo']}" if rec.get("redirected") else ""
        print(f"[{i}/{len(repos)}] {flag}{r}{redir}{gate}", flush=True)

    Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=1),
                              encoding="utf-8")

    bad = [r for r in results if not r.get("ok")]
    print(f"\n可用 {len(results)-len(bad)} / {len(results)}", file=sys.stderr)
    print("不可用:", *[f"{r['repo']}({r.get('why','issues off/archived')})" for r in bad],
          sep="\n  ", file=sys.stderr)
    print(f"\nsaved: {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
