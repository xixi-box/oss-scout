"""自动修复流水线（用户裁定 2026-10-25）：

    Worker 粗筛 → AI 细筛（issue_scorer）→ A 级 → 实时复检（Playbook §0.2）
    → PolicyEngine 合规门 → repair（OpenCode 修复+回归）→ 质量闸 → push 分支 + PR

硬边界（不逾越）：
- 每日 1 个新认领（ClaimLedger + worklog 双闸）；闸门池绝不直接 PR；
- 复检任一竞争信号 → aborted_competed，换下一条；
- 证据链不完整（base 未复现 / 补丁未通过 / 质量闸失败）→ 不开 PR；
- 未知仓库 / observe 车道 → 不动作。

由 dashboard 启动时挂后台线程（默认间隔检查 consume 窗口），也可手动触发（界面按钮）。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from ..repo_policy import PolicyEngine
from ..schemas import Job
from ..state_store import ClaimLedger, Worklog
from ..git_host import recheck_candidate
from .repair import run_repair
from .quality_gate import verify_result


def log(root: Path, action: str, target: str, result: str) -> None:
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "action": f"pipeline_{action}",
           "target": target, "result": result[:500]}
    with (root / "state" / "worklog.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def mark_candidate(root: Path, key: str, status: str) -> None:
    """pipeline 终态写回候选（原子写），避免同一候选无限重试。"""
    p = root / "queue" / "candidates.json"
    try:
        q = json.loads(p.read_text(encoding="utf-8"))
        if key in q:
            q[key]["status"] = status
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(q, ensure_ascii=False, indent=1), encoding="utf-8")
            json.loads(tmp.read_text(encoding="utf-8"))
            import os
            os.replace(tmp, p)
    except Exception:  # noqa: BLE001
        pass


def candidate_pipeline(root: Path, entry: dict, *,
                       max_files: int = 6) -> dict:
    """对单条候选执行：复检 → 合规 → 修复 → PR。返回动作摘要。"""
    root = Path(root)
    repo, number = entry["repo"], entry["number"]
    key = entry["id"]
    summary = {"id": key, "steps": []}

    eng, err = PolicyEngine.load(root)
    if err is not None:
        summary["abort"] = f"policy fail-closed: {err}"
        return summary
    lane = eng.repo_lane(repo)
    wl = Worklog(root / "state" / "worklog.jsonl")

    # ---- 1. 车道门 ----
    if lane in ("unknown", "observe"):
        summary["abort"] = f"lane={lane} no action"
        return summary
    summary["steps"].append(f"lane={lane}")

    # ---- 2. 实时复检（Playbook §0.2，不用队列快照）----
    recheck = recheck_candidate(repo, number)
    if recheck is None or not recheck.complete:
        summary["abort"] = f"recheck incomplete: {recheck.errors if recheck else 'None'}"
        log(root, "abort", key, summary["abort"])
        mark_candidate(root, key, "recheck_failed")  # 网络类失败：下轮可再试
        return summary
    if not recheck.issue_open or not recheck.unassigned or recheck.has_claim_signal \
            or recheck.has_cross_ref_pr or recheck.open_pr_hits:
        summary["abort"] = (f"competed: open={recheck.issue_open} "
                            f"unassigned={recheck.unassigned} "
                            f"claim={recheck.has_claim_signal} "
                            f"xref={recheck.has_cross_ref_pr} hits={recheck.open_pr_hits[:1]}")
        log(root, "abort", key, summary["abort"])
        mark_candidate(root, key, "aborted_competed")
        return summary
    summary["steps"].append("recheck ok")

    # ---- 3. 每日认领闸 + 闸门池规则 ----
    ledger = ClaimLedger(root / "state" / "scout_v2.sqlite3")
    if lane == "gated":
        # 闸门池：先评论对齐等指派——自动化只记录，绝不自动 PR
        summary["abort"] = "gated lane: comment-alignment flow only (no auto PR)"
        log(root, "abort", key, summary["abort"])
        ledger.close()
        return summary
    ok, reason = ledger.try_claim(eng.tz_name, key)
    if not ok:
        summary["abort"] = f"claim budget: {reason}"
        ledger.close()
        return summary
    wl.append({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "action": "claim",
               "target": key, "source": "auto-pipeline"})
    summary["steps"].append("claimed (daily budget)")

    def _abort_release(msg: str) -> dict:
        """失败路径统一：释放当日认领、close ledger、记审计。"""
        ledger.release_day()
        ledger.close()
        summary["abort"] = msg
        log(root, "abort", key, msg + "; claim released")
        return summary

    # ---- 4. 拿 base SHA + clone ----
    head = None
    for attempt in range(3):
        head = subprocess.run(["gh", "api", f"repos/{repo}/commits/HEAD", "--jq", ".sha"],
                              capture_output=True, text=True, encoding="utf-8")
        if head.returncode == 0:
            break
        time.sleep(min(30, (attempt + 1) * 10))
    if head.returncode != 0:
        return _abort_release(f"head sha failed: {head.stderr[:120]}")
    base_sha = head.stdout.strip()
    tmp = Path(tempfile.mkdtemp(prefix="pipe-"))
    clone = tmp / "repo"
    cl = subprocess.run(["git", "clone", "--quiet", "--filter=blob:none",
                         f"https://github.com/{repo}.git", str(clone)],
                        capture_output=True, text=True, timeout=900)
    co = subprocess.run(["git", "-C", str(clone), "checkout", "--quiet", "--detach", base_sha],
                        capture_output=True, text=True)
    if cl.returncode != 0 or co.returncode != 0:
        return _abort_release(f"clone/checkout failed: {(co.stderr or cl.stderr)[:150]}")

    # ---- 5. repair（OpenCode 修复 + base/补丁证据链）----
    out = root / "artifacts" / f"pipe-{key.replace('/', '_').replace('#', '_')}-{int(time.time())}"
    job = Job(mode="repair", repo=repo, base_sha=base_sha,
              idempotency_key=f"repair:{repo}:{base_sha}:{number}",
              test_cmd=entry.get("test_cmd") or [],
              context=f"{entry.get('title', '')}\n\n{entry.get('body_excerpt', '')}")
    if not job.test_cmd:
        # 从 issue 正文/仓库结构推断回归测试：pytest 文件若存在则用它
        guess = (entry.get("body_excerpt") or "")
        import re as _re
        m = _re.search(r"([\w/]+\.py)[^\n]{0,40}(?:fails|failing|失败)", guess, _re.I)
        if m and (clone / m.group(1)).is_file():
            tf = m.group(1)
            job.test_cmd = [sys.executable, "-m", "pytest", tf, "-q"]
            summary["steps"].append(f"test_cmd inferred: pytest {tf}")
        else:
            _r = _abort_release("no test_cmd and none inferable (need failing test first)")
            mark_candidate(root, key, "aborted_no_test")
            return _r
    res = run_repair(job, clone, out, test_cmd=list(job.test_cmd),
                     context=job.context, env_base=None)  # None=继承完整环境（脱敏在 sanitized_env 内做）

    # ---- 6. 质量闸 ----
    report = verify_result(res, job)
    if not (res.outcome == "succeeded" and res.reproduced_on_base
            and res.fixed_on_patch and report.ok):
        _r = _abort_release(
            f"evidence chain incomplete: {res.outcome} repro={res.reproduced_on_base} "
            f"fixed={res.fixed_on_patch} gate={report.errors or 'PASS'}")
        _r["repair"] = {"outcome": res.outcome, "repro": res.reproduced_on_base,
                        "fixed": res.fixed_on_patch}
        mark_candidate(root, key, "aborted_no_evidence")
        return _r
    summary["steps"].append("repair + quality gate ok")

    # ---- 7. push 分支 + PR（Own repo 或被允许的开放池）----
    branch = f"fix/issue-{number}-{base_sha[:7]}"
    subprocess.run(["git", "checkout", "-q", "-b", branch], cwd=clone, check=True)
    with (out / "change.patch").open("r", encoding="utf-8") as f:
        ap = subprocess.run(["git", "apply", "--whitespace=nowarn", "-"], cwd=clone,
                            stdin=f, capture_output=True, text=True)
    if ap.returncode != 0:
        return _abort_release("patch apply on branch failed")
    subprocess.run(["git", "add", "-A"], cwd=clone, check=True)
    subprocess.run(["git", "-c", "user.name=xixi-box", "-c", "user.email=wangshun-cn@qq.com",
                    "commit", "-qm", f"fix: address #{number}"], cwd=clone, check=True)
    push = subprocess.run(["git", "push", "-q", "origin", branch], cwd=clone,
                          capture_output=True, text=True)
    if push.returncode != 0:
        return _abort_release(f"push failed: {push.stderr[:150]}")

    gh_pr = subprocess.run(
        ["gh", "pr", "create", "--repo", repo, "--head", branch,
         "--title", f"fix: {entry.get('title', '')[:60]}",
         "--body", (f"Fixes #{number}\n\nWhat: minimal fix for the reported issue.\n\n"
                    f"Tests: failing test on base {base_sha[:12]}, passes after patch "
                    "(evidence in artifacts).")],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if gh_pr.returncode != 0:
        return _abort_release(f"pr create failed: {gh_pr.stderr[:150]}")
    pr_url = gh_pr.stdout.strip()
    summary["pr"] = pr_url
    log(root, "pr_open", key, f"{pr_url} (auto pipeline)")
    mark_candidate(root, key, "pr_open")
    ledger.close()
    return summary


def drain_ai_graded(root: Path, limit: int = 3, dry_run: bool = True) -> list[dict]:
    """扫描候选池：AI 评 A 且规则 A/B、未处理过的 → 逐条走 pipeline。

    dry_run=True 只报告"会处理哪些"，不做任何写动作。
    """
    root = Path(root)
    q = json.loads((root / "queue" / "candidates.json").read_text(encoding="utf-8"))
    picked = []
    for key, e in q.items():
        if len(picked) >= limit:
            break
        if e.get("status") not in ("new", "queued", "queued_hist", "recheck_failed"):
            continue   # 其余状态（aborted_*/pr_open）为终态，不重试
        ai = e.get("ai_grade") or {}
        if ai.get("grade") != "A" or e.get("class") not in ("A", "B"):
            continue
        picked.append(e)
    if dry_run:
        return [{"id": e["id"], "repo": e["repo"], "number": e["number"],
                 "ai": (e.get("ai_grade") or {}).get("grade"),
                 "rule": e.get("class"), "dry_run": True} for e in picked]
    out = []
    for e in picked:
        out.append(candidate_pipeline(root, e))
    return out
