"""全池最终审视：三个指标 + 得分 + 自动化阻碍（只读）。

阻碍分四类：
  AI_POLICY   明文禁止自主 agent / bulk agent-driven contributions → 必须移出
  CLA_MANUAL  CLA 必须人工网页签（cla-assistant / Google 表单）→ 预检拦截
  CLA_AUTO    comment_sign 类，代码能自动签 → 无阻碍
  ASSIGN      必须被指派才能开 PR → 只能等
"""
from __future__ import annotations

import base64
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scout_v2.repo_score import grade, total  # noqa: E402

POLICY_FILES = ["AI_POLICY.md", ".github/AI_POLICY.md", "CONTRIBUTING.md",
                ".github/CONTRIBUTING.md", "docs/CONTRIBUTING.md"]
# 只保留**语义明确**的禁用语。泛词（"we do not accept"、"will be closed"）
# 会误伤 valkey/firecracker 的"不接受匿名贡献者"和 pip 的普通措辞
# ——它们讲的是必须用真实姓名，与 agent 无关（2026-11-03 实测误报 6 个）。
BLOCK_RE = ("autonomous agents are not allowed",
            "do not allow autonomous",
            "not allowed to be used for contributing",
            "bulk, agent-driven contributions",
            "must be assigned to the issue",
            "not assigned to the linked issue",
            "require-issue-link")


def gh(*args, timeout=45):
    try:
        r = subprocess.run(["gh", *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except (subprocess.TimeoutExpired, OSError):
        return ""


def fetch(repo, path):
    raw = gh("api", f"repos/{repo}/contents/{path}")
    if not raw:
        return ""
    try:
        return base64.b64decode(json.loads(raw)["content"]).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""


def rate(repo):
    raw = gh("api", f"repos/{repo}/pulls?state=closed&per_page=100"
                    f"&sort=updated&direction=desc")
    try:
        prs = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return None
    merged = [p for p in prs if p.get("merged_at")]
    if not merged:
        return None
    times = sorted(datetime.fromisoformat(p["merged_at"].replace("Z", "+00:00"))
                   for p in merged)
    span = max((times[-1] - times[0]).total_seconds() / 86400, 1)

    def fork(p):
        full = str(((p.get("head") or {}).get("repo") or {})
                   .get("full_name") or "").lower()
        return bool(full) and full != repo.lower()

    ext = [p for p in merged if fork(p)]
    lat = []
    for p in ext[:40]:
        try:
            a = datetime.fromisoformat(p["created_at"].replace("Z", "+00:00"))
            b = datetime.fromisoformat(p["merged_at"].replace("Z", "+00:00"))
            lat.append((b - a).total_seconds() / 3600)
        except (ValueError, KeyError):
            continue
    lat.sort()
    med = lat[len(lat) // 2] if lat else None
    return {"share": round(len(ext) / len(merged) * 100),
            "per_week": round(len(ext) / span * 7, 1),
            "median_h": round(med, 1) if med is not None else None}


def main():
    cfg = yaml.safe_load((ROOT / "configs" / "repositories.yaml")
                         .read_text(encoding="utf-8"))
    gates = yaml.safe_load((ROOT / "configs" / "pr_gates.yaml")
                           .read_text(encoding="utf-8"))
    cla = gates.get("cla") or {}
    pool = [r for r in cfg["repositories"] if not r.startswith("xixi-box/")]

    rows = []
    for repo in pool:
        r = rate(repo) or {}
        score = total(r.get("share", 0), r.get("median_h"), r.get("per_week", 0)) \
            if r else 0.0

        obstacles = []
        for path in POLICY_FILES:
            t = fetch(repo, path)
            if not t:
                continue
            low = t.lower()
            hits = [p for p in BLOCK_RE if p in low]
            if hits:
                obstacles.append("AI_POLICY:" + hits[0])
                break

        req = cla.get(repo) or {}
        svc = req.get("service")
        if svc == "comment_sign":
            obstacles.append("CLA_AUTO")
        elif svc in ("cla-assistant", "google", "microsoft"):
            obstacles.append(f"CLA_MANUAL:{svc}")

        rows.append({"repo": repo, "score": score, "grade": grade(score),
                     **r, "obstacles": obstacles})
        flag = "!" if any(o.startswith("AI_POLICY") for o in obstacles) else " "
        print(f'  {flag} {repo:40s} {score:5.1f} {" ".join(obstacles) or "clean"}')
        time.sleep(0.2)

    rows.sort(key=lambda d: -d["score"])
    (ROOT / "pool_audit.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")

    print("\n\n| # | 仓库 | 分 | 档 | 外部合并率 | 中位 | /周 | 自动化 |")
    print("|---|---|---|---|---|---|---|---|")
    for i, d in enumerate(rows, 1):
        ms = f'{d["median_h"]}h' if d.get("median_h") is not None else "—"
        obs = d["obstacles"]
        if any(o.startswith("AI_POLICY") for o in obs):
            tag = "🚫 禁用"
        elif any(o.startswith("CLA_MANUAL") for o in obs):
            tag = "⚠️ 需人工签 CLA"
        elif "CLA_AUTO" in obs:
            tag = "✅ 自动签"
        else:
            tag = "✅ 无阻碍"
        print(f'| {i} | {d["repo"]} | {d["score"]} | {d["grade"]} | '
              f'{d.get("share", "—")}% | {ms} | {d.get("per_week", "—")} | {tag} |')

    bad = [d for d in rows if any(o.startswith("AI_POLICY") for o in d["obstacles"])]
    manual = [d for d in rows if any(o.startswith("CLA_MANUAL") for o in d["obstacles"])]
    print(f"\n合计 {len(rows)} 个 | 禁用 {len(bad)} | 需人工签 CLA {len(manual)} "
          f"| 全自动 {len(rows) - len(bad) - len(manual)}")


if __name__ == "__main__":
    main()
