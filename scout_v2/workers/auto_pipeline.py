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

# 推送目标：对第三方仓库我们没有写权限，分支一律推自己的 fork，再向原仓库发 PR。
# （实测 anomalyco/opencode 的 permissions.push = false）
FORK_OWNER = "xixi-box"


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
    # worklog 是唯一审计账本，写日志失败不该让调用方崩掉：暂存这类状态变更
    # 在 queue 里已经落盘，日志只是留痕。
    try:
        (root / "state").mkdir(parents=True, exist_ok=True)
        with (root / "state" / "worklog.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def mark_candidate(root: Path, key: str, status: str,
                   extra: dict | None = None) -> None:
    """pipeline 终态写回候选（原子写），避免同一候选无限重试。

    extra：一并写入的附加字段（如 recheck_failures 计数）。
    """
    p = root / "queue" / "candidates.json"
    try:
        q = json.loads(p.read_text(encoding="utf-8"))
        if key in q:
            q[key]["status"] = status
            q[key]["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            if extra:
                q[key].update(extra)
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
    # typing-only 仓库：测试是 mypy/pyright 的静态断言，不是运行期用例。
    # AI 生成的 pytest 文件在这种仓库里收集不到任何用例（no tests ran），
    # 防护会把整轮判为"环境不可用"并拒绝产出补丁——这是对的，但白烧一整个
    # Actions run。这里直接返回 None，让调用方在派发前就放弃。
    # 实测 2026-10-28：sqlalchemy#13731（declared_attr.cascading 的 typing 问题）
    # 走 pytest 路径必然 no tests ran。
    if "test/typing" in names or "typing" in names:
        return None
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
            # 不能用 sys.executable：那是**控制器**的 Python 绝对路径，
            # 发到 Modal/Actions 上根本不存在，base 测试直接 exit 127
            # "command likely not runnable"（2026-10-28 实测）。用 PATH 里的名字，
            # 由执行环境自己解析。
            cmd = ["python3", "-m", "pytest", rel, "-q"]
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

    # Actions：在**我们自己的**仓库里跑（目标仓库只是只读输入）。
    # 2026-10-28 修正：旧实现要求目标仓库里有我们的 workflow，不可行。
    actions_enabled = bool(feats.get("actions_backend"))
    actions_eligible = False
    if actions_enabled:
        try:
            be = ActionsBackend(enabled=True,
                                runner_repo=_actions_runner_repo(root),
                                workflow=_actions_workflow_name(root),
                                workflow_exists=True)
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
    return "oss-scout-repair.yml"


def _actions_runner_repo(root: Path) -> str:
    """执行 repair 的仓库（我们自己的）：configs/v2.yaml 覆盖，默认 xixi-box/oss-scout。"""
    import yaml
    for cfg in (root / "configs" / "v2.yaml",
                Path(os.environ.get("OSSCOUT_DASHBOARD_ROOT", "/data")) / "configs" / "v2.yaml"):
        if not cfg.exists():
            continue
        try:
            v2 = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            continue
        repo = ((v2.get("features") or {}).get("actions_runner_repo") or "").strip()
        if repo:
            return repo
    return "xixi-box/oss-scout"


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
        # 沙箱会执行目标仓库自己的测试代码，所以绝不向它注入任何 GitHub 凭据。
        # 生产目标（config.json 全部 19 个仓库）与 fixture 仓库都是公开的，
        # 匿名 https clone 即可；私有仓库请显式设 OSSCOUT_GIT_TOKEN 并在
        # core.ensure_repo 里走 strip-after-clone 通道，不要复活 GH_TOKEN 回落。
        image_path = Path(__file__).resolve().parents[2] / "docker" / "Dockerfile.worker"
        be = ModalSandboxBackend(guard, dockerfile=image_path,
                                 max_task_minutes=int(mb.get("max_task_minutes", 30)),
                                 extra_env=extra_env)
        with _no_proxy_context():   # Modal SDK 不能经 HTTP 代理
            remote_id = be.submit(job.to_dict())
        return "modal", remote_id, {"backend_obj": be, "note": decision.reason}

    be = ActionsBackend(enabled=True, runner_repo=_actions_runner_repo(root),
                        workflow=_actions_workflow_name(root), workflow_exists=True)
    return "github_actions", be.submit(job.to_dict()), {"backend_obj": be, "note": decision.reason}


def _collect(root: Path, backend: str, remote_id: str, extra: dict, timeout_s: int = 5400) -> dict:
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
    if not isinstance(env, dict):
        return {"result": None, "patch": None, "test_report": None, "raw": env}
    # 两个后端回来的形状不同：
    #   Modal  -> {"result": {...}, "patch": "...", "test_report": {...}}
    #   Actions-> 摊平的 result dict（含 job_id/outcome/...），patch 与 test_report
    #             作为同级键一起回来（无 artifact 时从 stdout 信封解析）。
    # 早期实现只认 "result" 键，Actions 的 patch 因此被当成 None 丢掉，配额一满就
    # 无法开 PR（2026-10-28 实测）。
    if "result" in env:
        return {"result": env.get("result"), "patch": env.get("patch"),
                "test_report": env.get("test_report"), "raw": env}
    patch = env.get("patch")
    report = env.get("test_report")
    publish = env.get("publish")
    result = {k: v for k, v in env.items()
              if k not in ("patch", "test_report", "raw", "publish")}
    return {"result": result, "patch": patch, "test_report": report,
            "publish": publish, "raw": env}


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
        # 必须写终态，否则状态留在 new/queued，drain 每轮都会重新取到它。
        # 2026-10-30 实测：修复循环改成 60 秒后，旧名单遗留的
        # anomalyco/opencode、deepset-ai/haystack 候选每分钟被重试一次，
        # 每次都 lane=unknown 空转。
        mark_candidate(root, key,
                       "out_of_scope" if lane == "unknown" else "observe_only",
                       extra={"abort_reason": summary["abort"]})
        return summary
    summary["steps"].append(f"lane={lane}")

    # ---- 实时复检（Playbook §0.2）----
    # 竞争 PR 搜索只用标题里的根因词；label（如 triaging）不是根因描述，
    # 用它搜 PR 只会制造无意义查询并放大失败面（2026-10-27 实测）。
    # 优先取标识符类词（含 _ / . 的模块名、函数名）：实测 ansible#87799 的竞争
    # PR 标题是 "mount_facts: strip blkid UUID lookup output"，按普通词搜不到，
    # 按模块名 mount_facts 才能命中。
    raw_words = [w for w in re.split(r"[^a-zA-Z0-9_.-]+",
                                     str(entry.get("title", "")))
                 if len(w) >= 5 and not w.isdigit()]
    ident = [w for w in raw_words if "_" in w or "." in w]
    title_words = (ident + [w for w in raw_words if w not in ident])[:3]
    recheck = recheck_candidate(repo, number, root_cause_keywords=title_words or None)
    if recheck is None or not recheck.complete:
        summary["abort"] = f"recheck incomplete: {recheck.errors if recheck else 'None'}"
        log(root, "abort", key, summary["abort"])
        # 复检失败会重试（状态 recheck_failed 在 drain 的候选集里）。修复循环
        # 现在每 60 秒一轮，如果 GitHub 持续抖动就会每分钟打一次。记失败次数，
        # 超过 3 次就标 escalated 不再自动重试，避免无意义刷 API。
        n = int(entry.get("recheck_failures") or 0) + 1
        mark_candidate(root, key, "escalated" if n >= 3 else "recheck_failed",
                       extra={"recheck_failures": n})
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

    # 这里原本有一道 pr_invited() 检查（查仓库要不要 invite label 才许开 PR）。
    # 2026-10-30 删除：门禁已经在**挑选仓库**这一步完成了 —— 会关闭未受邀 PR 的
    # 仓库根本不进 configs/repositories.yaml，也就不会被发现。留着它是对 39 个
    # 仓库恒真的空检查（实测空 labels 下 39/40 直接返回 True），而且是带状态的
    # 运行时判断：它曾在 labels 取不到时两头出错（该拒的放行、该放的拒掉）。
    # 约束归位到加仓库时：pr_gates.yaml 里 requires_invite: true 的仓库不该入池。

    summary["steps"].append("recheck ok")

    # ---- 认领闸 + 闸门池 ----
    ledger = ClaimLedger(root / "state" / "scout_v2.sqlite3")
    if lane == "gated":
        summary["abort"] = "gated lane: comment-alignment flow only (no auto PR)"
        log(root, "abort", key, summary["abort"])
        ledger.close()
        return summary
    # 每日上限由策略层配置（max_new_claims_per_business_day，0=不限）
    ok, reason = ledger.try_claim(eng.tz_name, key,
                                  max_per_day=eng.max_claims_per_day)
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
              # 给的上下文要足够 agent 自己判断：完整标题 + 正文（截到 6000，
              # 之前 3000 会砍掉复现步骤和预期行为）
              context=(f"{entry.get('title', '')}\n\n"
                       f"URL: https://github.com/{repo}/issues/{number}\n\n"
                       f"{(entry.get('body_excerpt') or '')[:6000]}"),
              max_estimated_compute_usd=0.5)
    # 测试框架由 agent 现场判断（2026-10-28）：控制器不再预设 pytest。
    # 之前在这里硬编码探测，"探测不到就放弃"，等于把能力锁死在 Python+pytest。
    # 现在只把 issue 交给 agent，它自己看 Cargo.toml / package.json / 测试目录。
    summary["steps"].append("agent decides test framework (no preset)")
    # 模型名由控制器解析后随 job 下发：worker 镜像里没有 configs/，
    # 无法自己读 model_policy.yaml。
    try:
        from .repair import resolve_model
        job.model = resolve_model()
    except Exception as e:  # noqa: BLE001
        return _abort_release(f"no model resolved: {str(e)[:160]}")
    # 路由器会选后端；只有 Actions 能自己写回（runner 有稳定出网 + PAT），
    # Modal 产出的补丁仍由控制器发布。这里先按路由结论决定要不要带 publish 配置。
    try:
        decision = _build_router(root, job, eng).route(job.to_dict())
    except Exception as e:  # noqa: BLE001
        return _abort_release(f"routing failed: {str(e)[:160]}")
    if decision.backend == "github_actions":
        job.publish = _build_publish_cfg(repo, number, base_sha, entry)
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

    # ---- 发布：优先用 runner 侧已完成的推送 ----
    # Actions 后端在 payload 带 publish 配置时会自己 push+PR，并把结果放进信封。
    # 这样 VPS 完全不碰 GitHub 写接口，也就没有出网单点。Modal 后端仍由控制器发布。
    runner_pr = _runner_published(collected)
    if runner_pr is not None:
        if runner_pr.get("ok"):
            pr_url = runner_pr.get("pr") or ""
            summary["steps"].append("published on runner")
        else:
            summary["abort"] = f"runner publish failed: {runner_pr.get('error', '')[:180]}"
            return _abort_release(summary["abort"])
    else:
        # ---- 控制器侧发布（Modal 后端 / runner 未配置 publish）----
        pr_url = _publish_pr(root, entry, base_sha, patch_file, summary)
        if pr_url is None:
            return _abort_release(summary.get("abort", "publish failed"))
    if not pr_url:
        return _abort_release("publish reported success but no PR url")
    summary["pr"] = pr_url
    log(root, "pr_open", key, f"{pr_url} (auto pipeline, backend={backend})")
    mark_candidate(root, key, "pr_open")
    ledger.close()
    return summary


def _runner_published(collected: dict) -> dict | None:
    """从后端回传里取出 runner 侧发布结果；没配 publish 时返回 None。"""
    pub = collected.get("publish")
    if isinstance(pub, dict):
        return pub
    raw = collected.get("raw")
    if isinstance(raw, dict) and isinstance(raw.get("publish"), dict):
        return raw["publish"]
    return None


def _redact_secret(text: str, secret: str) -> str:
    """运行日志/摘要里绝不能出现令牌原文。"""
    if not secret:
        return text or ""
    return (text or "").replace(secret, "***")


def _git_retry(args: list[str], *, timeout: int = 180, attempts: int = 3,
               secret: str = ""):
    """带重试的 git 调用。

    2026-10-28 实测：VPS 经代理连 github.com 偶发整段不通（一次 clone 卡了
    129s 后报 "Could not connect"），而同一命令重跑只需 2s。这类抖动不该
    让一条已经验证通过的修复作废——补丁还在本地，重试即可。

    TimeoutExpired 里的 cmd 含 URL（可能带 token），异常文本会直接进 traceback，
    所以超时也转成普通失败结果返回，不在调用方抛出去。
    """
    last = None
    for attempt in range(attempts):
        try:
            last = subprocess.run(["git"] + args, capture_output=True, text=True,
                                  timeout=timeout)
        except subprocess.TimeoutExpired:
            last = subprocess.CompletedProcess(
                args, 124, "",
                f"git {' '.join(args[:2])} timed out after {timeout}s")
            if secret:
                last.stderr = _redact_secret(last.stderr, secret)
        if last.returncode == 0:
            return last
        if attempt + 1 < attempts:
            time.sleep(5 * (attempt + 1))
    return last


def _fork_of(repo: str) -> str:
    """目标仓库对应的 fork（推送目标）。

    对第三方仓库我们没有 push 权限（实测 anomalyco/opencode: permissions.push
    = false），分支必须推到自己的 fork，再向原仓库发 PR。已经在 xixi-box 下的
    仓库（fixture）直接返回自身。
    """
    owner, name = repo.split("/", 1)
    if owner.lower() == FORK_OWNER:
        return repo
    return f"{FORK_OWNER}/{name}"


def _build_publish_cfg(repo: str, number: int, base_sha: str,
                       entry: dict) -> dict:
    """构造 runner 侧发布配置。

    PR 文案由控制器定稿而不是让 worker 自由发挥：AGENTS.md §6 要求对外文字
    精炼、就事论事、不写 AI 长文（opencode 等仓库会直接忽略长描述）。这里只给
    事实：修了什么、怎么验证的。
    """
    push_repo = _fork_of(repo)
    title = str(entry.get("title") or "").strip()
    # 仓库普遍用 conventional commits；我们没有足够信息判定 type，用最中性的 fix。
    # 标题不截断：2026-10-30 实测 [:70] 把 "...point to the line where the
    # variable is defined" 切成了 "...where the"，PR 标题成了半句话。
    # issue 标题本身就是最贴切的 PR 标题，GitHub 也不限制长度。
    pr_title = f"fix: {title}" if title else f"fix: resolves #{number}"
    # 描述里不写 "Root cause: <issue 标题>"：组 job 时还没跑出结果，能填的只有
    # 标题本身，于是同一句话出现两遍，读起来像假装做了归因。真正的根因由 agent
    # 产出，runner 发布时用 result.json 里的 root_cause 补上（见
    # publish_remote._rewrite_body）。这里只留能确证的事实：关联哪个 issue、
    # 在哪个 base 上验证过。
    body_lines = [
        f"Fixes #{number}",
        "",
        f"Verified by running the failing test at `{base_sha[:12]}` (fails) and "
        f"again with this patch applied (passes).",
    ]
    return {
        "push_repo": push_repo,
        "upstream": repo,
        "branch": f"fix/issue-{number}-{base_sha[:7]}",
        "issue_number": number,
        "title": pr_title,
        "body": "\n".join(x for x in body_lines if x is not None).strip(),
    }


def _publish_pr(root: Path, entry: dict, base_sha: str, patch_file: Path,
                summary: dict) -> str | None:
    """把后端产出的 patch 应用到分支、推到自己的 fork，并向原仓库开 PR。

    这里需要一次 clone（要 push），用 blob:none 浅克隆并立即清理；
    失败即返回 None（由调用方释放认领）。
    """
    repo = entry["repo"]
    number = entry["number"]
    push_repo = _fork_of(repo)
    tmp = Path(tempfile.mkdtemp(prefix="publish-"))
    try:
        clone = tmp / "repo"
        # 控制器是唯一写者，push 必须带凭据。用 token 拼 URL 而不是 credential
        # helper / .git-credentials：不落盘、不进 global config，进程结束即无痕。
        # 2026-10-28 实测：容器里 GH_TOKEN 有效但 git 没配置 helper，
        # 裸 git push 报 "could not read Username"，整条链就停在最后一步。
        token = (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
        # clone 上游（只读），push 到 fork（有写权限）
        push_url = (f"https://x-access-token:{token}@github.com/{push_repo}.git" if token
                    else f"https://github.com/{push_repo}.git")
        # clone 上游（公共仓库匿名即可，保证 base_sha 一定在）；push 走 fork。
        upstream_url = f"https://github.com/{repo}.git"
        cl = _git_retry(["clone", "--quiet", "--filter=blob:none", "--depth", "1",
                         upstream_url, str(clone)], timeout=180, secret=token)
        if cl.returncode != 0:
            summary["abort"] = f"publish clone failed: {_redact_secret(cl.stderr, token)[:150]}"
            return None
        # fork 作为推送 remote；origin 保持上游干净 URL（不残留 token）
        subprocess.run(["git", "-C", str(clone), "remote", "add", "fork", push_url],
                       capture_output=True, check=False)
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
        # push 到 fork；代理抖动会让它失败，重试而不是让验证过的修复作废
        push = None
        for attempt in range(3):
            try:
                push = subprocess.run(["git", "push", "-q", "fork", branch], cwd=clone,
                                      capture_output=True, text=True, timeout=300)
            except subprocess.TimeoutExpired:
                push = subprocess.CompletedProcess(
                    [], 124, "", f"git push timed out after 300s")
            if push.returncode == 0:
                break
            time.sleep(5 * (attempt + 1))
        if push.returncode != 0:
            summary["abort"] = f"push failed: {_redact_secret(push.stderr, token)[:150]}"
            return None
        # 补丁是我们自己验证过的产物，不是"AI 生成的建议"——但 PR 描述不能写成
        # 长篇说明（AGENTS.md §6：opencode 等仓库反感 AI 长文）。只说事实。
        body = (f"Fixes #{number}\n\n"
                f"Root cause: {entry.get('title', '')}\n\n"
                f"Verified by running the failing test at `{base_sha[:12]}` (fails) "
                f"and again with this patch applied (passes).\n")
        # 跨 fork 开 PR 时 --head 必须写 <owner>:<branch>
        head = f"{push_repo.split('/')[0]}:{branch}" if push_repo != repo else branch
        pr = subprocess.run(["gh", "pr", "create", "--repo", repo, "--head", head,
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

def _stage_candidates(root: Path, keys: list[str]) -> int:
    """把候选标成暂存，留到下一轮再处理。返回实际改动的条数。

    用于额度用完之后：ingest 一次扫到 10 条、今天只剩 1 个额度，剩下 9 条不该
    继续躺在 new 里被每轮反复扫描，也不该丢掉——标 staged 并记时间，drain 时
    会优先清暂存。
    """
    if not keys:
        return 0
    p = root / "queue" / "candidates.json"
    try:
        q = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return 0
    n = 0
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S")
    for key in keys:
        e = q.get(key)
        if not isinstance(e, dict) or e.get("status") not in ("new", "queued"):
            continue
        e["status"] = STAGED_STATUS
        e.setdefault("staged_at", now_iso)
        n += 1
    if n:
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(q, ensure_ascii=False, indent=1), encoding="utf-8")
        json.loads(tmp.read_text(encoding="utf-8"))
        os.replace(tmp, p)
        log(root, "stage", f"{n} candidates", "deferred (daily budget exhausted)")
    return n


STAGED_STATUS = "staged"          # 今天额度用完了，留到明天再处理
STAGE_TTL_DAYS = 3                # 暂存超过这个天数就丢，避免拿陈旧的 issue 去修


def _stage_age_days(entry: dict) -> float:
    """候选进入暂存状态多久了（天）。时间戳缺失按 0 处理（当作刚暂存）。"""
    ts = entry.get("staged_at") or entry.get("discovered_at") or ""
    if not ts:
        return 0.0
    try:
        then = time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return 0.0
    return max(0.0, (time.time() - then) / 86400.0)


def _order_candidates(entries: list[tuple[str, dict]]) -> list[tuple[str, dict]]:
    """暂存优先，且同组内先到先出。

    2026-10-30（用户裁定）：每天额度用完之后，当天新识别出的可处理 issue 不要
    丢，也不要挤掉明天——存起来，第二天**先清暂存**，暂存清完才接当天新扫的。
    这样额度紧张时优先消化积压，同时保证昨天的发现不会因为今天又扫到一堆新的
    就永远排不上。

    超过 STAGE_TTL_DAYS 的暂存直接丢弃：一个 issue 放了三天，多半已经被人修了、
    被关了，或者上下文已经变了；拿去修反而浪费额度并可能撞车。
    """
    staged, fresh = [], []
    for key, e in entries:
        if e.get("status") == STAGED_STATUS:
            if _stage_age_days(e) > STAGE_TTL_DAYS:
                continue
            staged.append((key, e))
        else:
            fresh.append((key, e))
    # 暂存组按进入时间正序（最早暂存的先做）；新候选按发现时间正序
    staged.sort(key=lambda kv: str(kv[1].get("staged_at")
                                   or kv[1].get("discovered_at") or ""))
    fresh.sort(key=lambda kv: str(kv[1].get("discovered_at") or ""))
    return staged + fresh


def drain_ai_graded(root: Path, limit: int = 3, dry_run: bool = True,
                    max_new_claims: int = 0) -> list[dict]:
    """扫描候选池：AI 判定可处理、无客观否决、未处理过的 → 逐条走后端 pipeline。

    2026-10-30（用户裁定）：入口只看 AI 的二元判断。规则打分那套加分表
    （reactions/龄/标签权重）已删除——它是在 AI 评分之前写的启发式，等于对
    AI 的判断再加一道更粗糙的复核，两层经常互相打架。规则只留客观事实否决
    （blockers 非空 = 已有 PR / 认领信号 / 纯文档 / 计费抱怨这类不需要判断的事实）。

    max_new_claims：当日还剩几个额度。额度耗尽时今天新识别出的候选标成
    staged（留到明天），而不是留在 new 里等着被同一轮反复扫描。
    """
    root = _data_root(root)
    qpath = root / "queue" / "candidates.json"
    q = json.loads(qpath.read_text(encoding="utf-8"))
    ACTIVE = ("new", "queued", "queued_hist", "recheck_failed", STAGED_STATUS)
    eligible: list[tuple[str, dict]] = []
    for key, e in q.items():
        if e.get("status") not in ACTIVE:
            continue
        if e.get("blockers"):
            continue
        # 新格式用 ai_verdict.usable；兼容旧的 ai_grade.grade == "A"
        verdict = e.get("ai_verdict") or {}
        usable = verdict.get("usable")
        if usable is None:
            usable = (e.get("ai_grade") or {}).get("grade") == "A"
        if usable is not True:
            continue
        eligible.append((key, e))

    ordered = _order_candidates(eligible)
    picked: list[dict] = []
    to_stage: list[str] = []
    for key, e in ordered:
        if len(picked) >= limit:
            # 额度（或本轮并发）用完：把今天新扫到的留到明天。
            # 已经在暂存里的不动——它的 staged_at 要保持原值，否则永远不会过期。
            if e.get("status") != STAGED_STATUS:
                to_stage.append(key)
            continue
        picked.append(e)

    if to_stage and not dry_run:
        for key in to_stage:
            q[key]["status"] = STAGED_STATUS
            q[key].setdefault("staged_at", time.strftime("%Y-%m-%dT%H:%M:%S"))
        tmp = qpath.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(q, ensure_ascii=False, indent=1), encoding="utf-8")
        json.loads(tmp.read_text(encoding="utf-8"))
        os.replace(tmp, qpath)
        log(root, "stage", f"{len(to_stage)} candidates",
            "deferred to next run: " + ", ".join(to_stage[:5]))

    if dry_run:
        return [{"id": e["id"], "repo": e["repo"], "number": e["number"],
                 "status": e.get("status"),
                 "ai": (e.get("ai_verdict") or {}).get("reason", "")[:60],
                 "dry_run": True} for e in picked]
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
