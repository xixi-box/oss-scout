"""自动修复流水线（用户裁定 2026-10-27）：修复必须跑在后端，不在 VPS 本地。

    Worker 粗筛 → AI 细筛 → A 级 → 实时复检 → 合规门 → **JobRouter 选后端**
    → Modal Sandbox / GitHub Actions 执行 repair → 后端回传 patch + 测试报告
    → 质量闸 → VPS 只做 push 分支 + 开 PR（唯一写者）

为什么不在 VPS 本地跑（2026-10-27 重构）：
- 本地 clone 第三方大仓库 + 装依赖会撑爆 2 核 40G 生产机（当天磁盘 100% 事故）；
- 本地环境没有目标项目的依赖/测试框架，修复与验证都不可信；
- 架构里本来就有 Modal/Actions 两个后端，旧实现绕过了它们（平行实现）。

硬边界（不逾越）：
- 每日 1 个新认领（ClaimLedger + worklog 双闸）；闸门池绝不直接 PR；
- 复检任一竞争信号 → aborted_competed；
- 证据链不完整（base 未复现 / 补丁未通过 / 质量闸失败）→ 不开 PR；
- 后端不可用且预算不足 → 暂停，不降级到本地执行。
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import shutil
import sys
import tempfile
import time
from pathlib import Path

from ..adapters.actions_backend import ActionsBackend
from ..adapters.modal_backend import ModalSandboxBackend, parse_envelope
from ..budget import BudgetGuard, ModalCliBilling
from ..git_host import recheck_candidate
from ..job_router import JobRouter, RouterConfig
from ..repo_policy import PolicyEngine
from ..schemas import Job, Result
from ..state_store import ClaimLedger, Worklog
from .issue_scorer import GoClient, load_scorer_config
from .quality_gate import verify_result


def _data_root(root: Path) -> Path:
    """数据根：queue/state 所在目录（容器里源码在 /app、数据在 /data）。"""
    root = Path(root)
    if (root / "queue" / "candidates.json").exists():
        return root
    alt = Path(os.environ.get("OSSCOUT_DASHBOARD_ROOT", "/data"))
    if (alt / "queue" / "candidates.json").exists():
        return alt
    return root


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
            q[key]["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(q, ensure_ascii=False, indent=1), encoding="utf-8")
            json.loads(tmp.read_text(encoding="utf-8"))
            os.replace(tmp, p)
    except Exception:  # noqa: BLE001 — 状态写入失败不影响已完成的动作
        pass


def _emergency_release(root: Path) -> None:
    """pipeline 崩溃时的兜底：释放当日认领，避免额度被僵尸占死。"""
    try:
        import datetime as _dt
        conn_ledger = ClaimLedger(root / "state" / "scout_v2.sqlite3")
        conn_ledger.release_day(_dt.date.today().isoformat())
        conn_ledger.close()
    except Exception:  # noqa: BLE001
        pass


def _probe_test_stack(repo: str, base_sha: str):
    """用 GitHub tree API 探测仓库结构与测试栈（不 clone）。

    返回 (lang, tpl, tests_dir, top_entries) 或 None（不支持/不可达）。
    """
    try:
        out = subprocess.run(
            ["gh", "api", f"repos/{repo}/git/trees/{base_sha}", "--jq",
             "[.tree[] | .path]"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        if out.returncode != 0:
            return None
        entries = json.loads(out.stdout or "[]")
    except Exception:  # noqa: BLE001
        return None
    names = set(entries)
    tests_dir = next((d for d in ("tests", "test", "spec") if d in names), None)
    top = sorted(names)[:12]
    if {"pyproject.toml", "setup.py", "requirements.txt"} & names:
        return "python", "python -m pytest {path} -q", tests_dir, top
    if "package.json" in names:
        return "node", "npx --no-install bun test {path}", tests_dir, top
    if "go.mod" in names:
        return "go", "go test {path}", tests_dir, top
    # 兜底：无标准清单文件但根目录有 Python 测试 → 仍按 Python 处理
    if any(str(n).startswith("test_") and str(n).endswith(".py") for n in names):
        return "python", "python -m pytest {path} -q", (tests_dir or "."), top
    return None


def _ai_generate_test_remote(job: Job, lang: str, tpl: str, tests_dir: str | None,
                             top_entries: list[str], summary: dict):
    """让 AI 生成回归测试内容（不落盘，随 job 下发）。

    返回 (test_cmd, rel_path, code)；失败返回 (None, None, None)。
    """
    try:
        cfg = load_scorer_config(_data_root(Path(__file__).resolve().parents[2]))
        if not cfg.get("api_key"):
            return None, None, None
        if lang == "python":
            lang_rule = ("Output ONLY a single Python file content using pytest "
                         "(no markdown fences, no explanations).")
            marker, ext = "def test", ".py"
        elif lang == "node":
            lang_rule = ("Output ONLY a single TypeScript test file using bun:test "
                         "(import {test, expect} from 'bun:test'). "
                         "No markdown fences, no explanations.")
            marker, ext = "test(", ".test.ts"
        else:
            lang_rule = "Output ONLY a single Go test file (package *_test)."
            marker, ext = "func Test", "_test.go"
        prompt = (
            "You are writing a MINIMAL regression test that must FAIL on the current code "
            "and PASS after the bug is fixed.\n"
            f"Repo: {job.repo} @ {job.base_sha[:12]}\n"
            f"Issue context:\n{job.context[:2500]}\n\n"
            f"Top-level entries: {', '.join(top_entries)}\n"
            f"Existing test dir: {tests_dir or 'none'}\n\n"
            f"{lang_rule} It must import from the repo's own modules and assert the "
            "behaviour described in the issue.\n\n"
            "IMPORTANT: This is a plain-text completion, NOT an agent session. You have no "
            "tools and cannot run commands. Do NOT emit tool-call syntax (no DSML, no "
            "function calls, no XML tags). Reply with the test file source only."
        )
        client = GoClient(key=cfg["api_key"],
                          model=cfg.get("model", "deepseek-v4.1-flash"), timeout=90)
        # 上游模型偶发 "Model is unavailable"：按序降级到其他可用模型
        ok, code, used = client.chat_with_fallback(
            prompt, fallbacks=["glm-5.3-flash", "glm-5.3", "deepseek-v4.1-flash"])
        text = str(code or "")
        ok_marker = marker in text or (lang == "node" and any(
            m in text for m in ("test(", "it(", "describe("))) or (
            lang == "python" and "def test" in text)
        if not ok:
            summary["steps"].append(f"AI gen failed: {text[:160]}")
            return None, None, None
        if not ok_marker:
            looks_toolcall = any(t in text for t in (
                "DSML", "invoke name=", "tool_call", "<function", "parameter name="))
            summary["steps"].append(
                "AI returned tool-call syntax instead of a test file"
                if looks_toolcall else f"AI output lacks test marker: {text[:120]}")
            return None, None, None
        code = str(code).strip()
        if code.startswith("```"):
            code = "\n".join(code.splitlines()[1:-1])
        rel = (tests_dir + "/" if tests_dir else "") + "test_ai_regression" + ext
        summary["steps"].append(f"AI-generated regression test: {rel} (model={used})")
        if lang == "python":
            cmd = [sys.executable, "-m", "pytest", rel, "-q"]
        else:
            cmd = tpl.format(path=rel).split()
        return cmd, rel, code
    except Exception as e:  # noqa: BLE001
        summary["steps"].append(f"AI gen error: {type(e).__name__}: {str(e)[:150]}")
        return None, None, None


# ---------------- 后端选择 ----------------

def _build_router(root: Path, job: Job, engine: PolicyEngine) -> JobRouter:
    """按 configs/v2.yaml 的路由表 + 各后端可用性构建路由器。"""
    import yaml
    cfg_path = root / "configs" / "v2.yaml"
    v2 = (yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}) if cfg_path.exists() else {}
    budget_cfg = ((v2.get("budget") or {}).get("modal") or {})
    feats = v2.get("features") or {}

    modal_enabled = bool(budget_cfg.get("enabled"))
    modal_budget_ok = False
    if modal_enabled:
        try:
            billing = ModalCliBilling()
            guard = BudgetGuard(
                soft_cap_usd=float(budget_cfg.get("monthly_soft_cap_usd", 25.0)),
                enabled=True, billing=billing,
                max_concurrent=int(budget_cfg.get("max_concurrent_sandboxes", 1)))
            modal_budget_ok = guard.can_start(job.max_estimated_compute_usd)[0]
        except Exception:  # noqa: BLE001 — 无法确认预算即视为不可用（fail-closed）
            modal_budget_ok = False

    # Actions：只有目标仓库存在我们的 worker 工作流时才 eligible
    actions_enabled = bool(feats.get("actions_backend"))
    actions_eligible = False
    if actions_enabled:
        try:
            be = ActionsBackend(enabled=True, repo=job.repo,
                                workflow=_actions_workflow_name(root),
                                workflow_exists=_repo_has_worker_workflow(job.repo))
            actions_eligible = be.eligible(job.to_dict())
        except Exception:  # noqa: BLE001
            actions_eligible = False

    return JobRouter(RouterConfig(
        modal_enabled=modal_enabled,
        actions_enabled=actions_enabled,
        modal_budget_ok=modal_budget_ok,
        actions_eligible=actions_eligible,
        routing=v2.get("routing"),
    ))


def _actions_workflow_name(root: Path) -> str:
    return "oss-scout-worker.yml"


def _repo_has_worker_workflow(repo: str) -> bool:
    """目标仓库（fork）里是否已有 oss-scout-worker.yml。"""
    try:
        out = subprocess.run(
            ["gh", "api", f"repos/{repo}/contents/.github/workflows/{_actions_workflow_name(root)}",
             "--jq", ".name"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        return out.returncode == 0 and bool(out.stdout.strip())
    except Exception:  # noqa: BLE001
        return False


def _no_proxy_context():
    """Modal SDK 走 gRPC/WebSocket，经 HTTP 代理会 "Could not connect to the
    Modal server"。提交 Sandbox / 读账单期间临时剥离代理变量。
    用法：with _no_proxy_context(): ...
    """
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        keys = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                "http_proxy", "https_proxy", "all_proxy")
        saved = {k: os.environ.pop(k) for k in keys if k in os.environ}
        try:
            yield
        finally:
            os.environ.update(saved)

    return _ctx()


def _submit_job(root: Path, job: Job, engine: PolicyEngine) -> tuple[str, str, dict]:
    """按路由提交 job。返回 (backend, remote_id, extra)。"""
    router = _build_router(root, job, engine)
    decision = router.route(job.to_dict())
    if decision.backend in ("pause", "local") or decision.backend not in (
            "modal", "github_actions"):
        raise RuntimeError(f"no eligible backend: {decision.reason}")

    if decision.backend == "modal":
        import yaml
        cfg_path = root / "configs" / "v2.yaml"
        v2 = (yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}) if cfg_path.exists() else {}
        mb = ((v2.get("budget") or {}).get("modal") or {})
        billing = ModalCliBilling()
        guard = BudgetGuard(
            soft_cap_usd=float(mb.get("monthly_soft_cap_usd", 25.0)), enabled=True,
            billing=billing, max_concurrent=int(mb.get("max_concurrent_sandboxes", 1)))
        extra_env = {}
        key = (os.environ.get("OPENCODE_API_KEY") or "").strip()
        if key:
            extra_env["OPENCODE_API_KEY"] = key
        image_path = Path(__file__).resolve().parents[2] / "docker" / "Dockerfile.worker"
        be = ModalSandboxBackend(guard, dockerfile=image_path,
                                 max_task_minutes=int(mb.get("max_task_minutes", 30)),
                                 extra_env=extra_env)
        with _no_proxy_context():   # Modal SDK 不能经 HTTP 代理
            remote_id = be.submit(job.to_dict())
        return "modal", remote_id, {"backend_obj": be, "note": decision.reason}

    be = ActionsBackend(enabled=True, repo=job.repo,
                        workflow=_actions_workflow_name(root), workflow_exists=True)
    return "github_actions", be.submit(job.to_dict()), {"backend_obj": be, "note": decision.reason}


def _collect(root: Path, backend: str, remote_id: str, extra: dict, timeout_s: int = 1800) -> dict:
    """轮询并收集后端结果。"""
    be = extra["backend_obj"]
    t0 = time.time()
    while True:
        with _no_proxy_context():
            st = be.poll(remote_id)
        state = st.get("state")
        if state != "running":
            break
        if time.time() - t0 > timeout_s:
            try:
                be.cancel(remote_id)
            except Exception:  # noqa: BLE001
                pass
            return {"outcome": "timed_out",
                    "unverified_items": [f"backend did not finish in {timeout_s}s"]}
        time.sleep(15)
    with _no_proxy_context():
        env = be.collect(remote_id)
    # Modal 返回信封 {result, patch, test_report}；Actions 返回 result dict
    if isinstance(env, dict) and "result" in env:
        return {"result": env.get("result"), "patch": env.get("patch"),
                "test_report": env.get("test_report"), "raw": env}
    return {"result": env, "patch": None, "test_report": None, "raw": env}


def _stage_patch_from_backend(root: Path, out_dir: Path, collected: dict) -> Path | None:
    """把后端回传的 patch 落到 artifacts（Sandbox 无持久卷）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    patch_text = collected.get("patch")
    if not patch_text:
        return None
    pf = out_dir / "change.patch"
    pf.write_text(patch_text, encoding="utf-8")
    return pf


# ---------------- 主流程 ----------------

def candidate_pipeline(root: Path, entry: dict, *, max_files: int = 6) -> dict:
    """单候选：复检 → 合规 → 后端修复 → 质量闸 → push + PR。"""
    root = _data_root(root)
    repo, number = entry["repo"], entry["number"]
    key = entry["id"]
    summary: dict = {"id": key, "steps": []}

    eng, err = PolicyEngine.load(root)
    if err is not None:
        summary["abort"] = f"policy fail-closed: {err}"
        return summary
    lane = eng.repo_lane(repo)
    wl = Worklog(root / "state" / "worklog.jsonl")

    if lane in ("unknown", "observe"):
        summary["abort"] = f"lane={lane} no action"
        return summary
    summary["steps"].append(f"lane={lane}")

    # ---- 实时复检（Playbook §0.2）----
    # 竞争 PR 搜索只用标题里的根因词；label（如 triaging）不是根因描述，
    # 用它搜 PR 只会制造无意义查询并放大失败面（2026-10-27 实测）
    title_words = [w for w in re.split(r"[^a-zA-Z0-9_.-]+",
                                       str(entry.get("title", "")))
                   if len(w) >= 5][:3]
    recheck = recheck_candidate(repo, number, root_cause_keywords=title_words or None)
    if recheck is None or not recheck.complete:
        summary["abort"] = f"recheck incomplete: {recheck.errors if recheck else 'None'}"
        log(root, "abort", key, summary["abort"])
        mark_candidate(root, key, "recheck_failed")
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

    # ---- 认领闸 + 闸门池 ----
    ledger = ClaimLedger(root / "state" / "scout_v2.sqlite3")
    if lane == "gated":
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
        ledger.release_day()
        ledger.close()
        summary["abort"] = msg
        log(root, "abort", key, msg + "; claim released")
        return summary

    # ---- base SHA ----
    head = None
    for attempt in range(3):
        head = subprocess.run(
            ["gh", "api", f"repos/{repo}/commits/HEAD", "--jq", ".sha"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        if head.returncode == 0:
            break
        time.sleep(min(30, (attempt + 1) * 10))
    if head is None or head.returncode != 0:
        return _abort_release(f"head sha failed: {(head.stderr if head else '')[:120]}")
    base_sha = head.stdout.strip()

    # ---- 组 job 并交给后端（VPS 不 clone）----
    out_dir = root / "artifacts" / f"pipe-{key.replace('/', '_').replace('#', '_')}-{int(time.time())}"
    job = Job(mode="repair", repo=repo, base_sha=base_sha,
              idempotency_key=f"repair:{repo}:{base_sha}:{number}",
              execution_scope="project_contribution",
              policy_snapshot_id=eng.snapshot_id(),
              context=f"{entry.get('title', '')}\n\n{(entry.get('body_excerpt') or '')[:3000]}",
              max_estimated_compute_usd=0.5)
    # 回归测试由 AI 生成后随 job 下发（后端执行时才落盘）：
    # 仓库结构用 GitHub tree API 探测，VPS 不 clone
    test_stack = _probe_test_stack(repo, base_sha)
    if not test_stack:
        return _abort_release("unsupported/unreachable test stack; cannot verify fix")
    lang, tpl, tests_dir, top_entries = test_stack
    summary["steps"].append(f"test stack: {lang}")
    cmd, rel, code = _ai_generate_test_remote(job, lang, tpl, tests_dir, top_entries, summary)
    if not cmd:
        return _abort_release("no test_cmd and none inferable (AI generation failed)")
    job.test_cmd = cmd
    if rel and code:
        job.ai_test_files = {rel: code}
    try:
        backend, remote_id, extra = _submit_job(root, job, eng)
    except Exception as e:  # noqa: BLE001
        return _abort_release(f"backend unavailable: {str(e)[:200]}")
    summary["steps"].append(f"backend={backend} ({extra.get('note', '')})")

    collected = _collect(root, backend, remote_id, extra)
    res = Result.from_dict(collected["result"]) if isinstance(collected.get("result"), dict) \
        else Result(job_id=job.job_id, backend=backend, outcome="failed",
                    base_sha=base_sha, unverified_items=["no result from backend"])
    if res.backend in ("", None):
        res.backend = backend
    patch_file = _stage_patch_from_backend(root, out_dir, collected)

    # ---- 质量闸 ----
    report = verify_result(res, job, artifact_dir=root)
    if not (res.outcome == "succeeded" and res.reproduced_on_base
            and res.fixed_on_patch and report.ok and patch_file):
        _r = _abort_release(
            f"evidence chain incomplete: {res.outcome} repro={res.reproduced_on_base} "
            f"fixed={res.fixed_on_patch} gate={report.errors or 'PASS'} "
            f"patch={'yes' if patch_file else 'no'}")
        _r["repair"] = {"backend": backend, "outcome": res.outcome,
                        "repro": res.reproduced_on_base, "fixed": res.fixed_on_patch,
                        "unverified": res.unverified_items[:3]}
        mark_candidate(root, key, "aborted_no_evidence")
        return _r
    summary["steps"].append("repair + quality gate ok")

    # ---- push 分支 + PR（VPS 是唯一写者）----
    pr_url = _publish_pr(root, entry, base_sha, patch_file, summary)
    if pr_url is None:
        return _abort_release(summary.get("abort", "publish failed"))
    summary["pr"] = pr_url
    log(root, "pr_open", key, f"{pr_url} (auto pipeline, backend={backend})")
    mark_candidate(root, key, "pr_open")
    ledger.close()
    return summary


def _publish_pr(root: Path, entry: dict, base_sha: str, patch_file: Path,
                summary: dict) -> str | None:
    """在 VPS 上把后端产出的 patch 应用到分支并开 PR。

    这里仍需要一次 clone（要 push），但只用 blob:none 浅克隆并立即清理，
    且失败即返回 None（由调用方释放认领）。
    """
    repo = entry["repo"]
    number = entry["number"]
    tmp = Path(tempfile.mkdtemp(prefix="publish-"))
    try:
        clone = tmp / "repo"
        cl = subprocess.run(["git", "clone", "--quiet", "--filter=blob:none", "--depth", "1",
                             f"https://github.com/{repo}.git", str(clone)],
                            capture_output=True, text=True, timeout=600)
        if cl.returncode != 0:
            summary["abort"] = f"publish clone failed: {cl.stderr[:150]}"
            return None
        branch = f"fix/issue-{number}-{base_sha[:7]}"
        subprocess.run(["git", "checkout", "-q", "-b", branch], cwd=clone, check=True)
        with patch_file.open("r", encoding="utf-8") as f:
            ap = subprocess.run(["git", "apply", "--whitespace=nowarn", "-"], cwd=clone,
                               stdin=f, capture_output=True, text=True)
        if ap.returncode != 0:
            summary["abort"] = f"patch apply failed: {ap.stderr[:150]}"
            return None
        subprocess.run(["git", "add", "-A"], cwd=clone, check=True)
        subprocess.run(["git", "-c", "user.name=xixi-box",
                        "-c", "user.email=wangshun-cn@qq.com",
                        "commit", "-qm", f"fix: {str(entry.get('title', ''))[:60]}"],
                       cwd=clone, check=True)
        push = subprocess.run(["git", "push", "-q", "origin", branch], cwd=clone,
                              capture_output=True, text=True, timeout=300)
        if push.returncode != 0:
            summary["abort"] = f"push failed: {push.stderr[:150]}"
            return None
        body = (f"Fixes #{number}\n\n"
                f"Root cause and fix produced by the oss-scout worker; verified by running the "
                f"failing test on base `{base_sha[:12]}` and again after the patch.\n\n"
                "Tests: see the worker's test report attached in the run artifacts.")
        pr = subprocess.run(["gh", "pr", "create", "--repo", repo, "--head", branch,
                             "--title", f"fix: {str(entry.get('title', ''))[:70]}",
                             "--body", body],
                            capture_output=True, text=True, encoding="utf-8", errors="replace",
                            timeout=180)
        if pr.returncode != 0:
            summary["abort"] = f"pr create failed: {pr.stderr[:150]}"
            return None
        return pr.stdout.strip()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------- 候选筛选 ----------------

def drain_ai_graded(root: Path, limit: int = 3, dry_run: bool = True) -> list[dict]:
    """扫描候选池：AI 评 A 且规则 A/B、未处理过的 → 逐条走后端 pipeline。"""
    root = _data_root(root)
    q = json.loads((root / "queue" / "candidates.json").read_text(encoding="utf-8"))
    picked = []
    for key, e in q.items():
        if len(picked) >= limit:
            break
        if e.get("status") not in ("new", "queued", "queued_hist", "recheck_failed"):
            continue
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
        try:
            out.append(candidate_pipeline(root, e))
        except Exception as ex:  # noqa: BLE001
            log(root, "crash", str(e.get("id", "")),
                f"unhandled: {type(ex).__name__}: {str(ex)[:200]}; releasing day claim")
            _emergency_release(root)
            out.append({"id": e.get("id", ""), "crash": f"{type(ex).__name__}: {str(ex)[:150]}"})
    return out
