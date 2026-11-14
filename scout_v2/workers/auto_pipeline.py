"""自动修复流水线（用户裁定 2026-10-27）：修复必须跑在后端，不在 VPS 本地。

    Worker 粗筛 → AI 细筛 → A 级 → 实时复检 → 合规门 → **JobRouter 选后端**
    → Modal Sandbox / GitHub Actions 执行 repair → 后端回传 patch + 测试报告
    → 质量闸 → VPS 只做 push 分支 + 开 PR（唯一写者）

为什么不在 VPS 本地跑（2026-10-27 重构）：
- 本地 clone 第三方大仓库 + 装依赖会撑爆 2 核 40G 生产机（当天磁盘 100% 事故）；
- 本地环境没有目标项目的依赖/测试框架，修复与验证都不可信；
- 架构里本来就有 Modal/Actions 两个后端，旧实现绕过了它们（平行实现）。

硬边界（不逾越）：
- 北京时间当天创建的 PR 最多占用 5 个名额，当天关闭且未合并可补位；
- 闸门池绝不直接开 PR；
- 机械粗筛命中拦截条件 → coarse_blocked；
- 证据链不完整（base 未复现 / 补丁未通过 / 质量闸失败）→ 不开 PR；
- 后端不可用且预算不足 → 暂停，不降级到本地执行。
"""
from __future__ import annotations

import datetime as dt

import json
import os
import re
import subprocess
import shutil
import tempfile
import time
from pathlib import Path

from ..adapters.actions_backend import ActionsBackend
from ..adapters.modal_backend import ModalSandboxBackend, parse_envelope
from ..budget import BudgetGuard, ModalCliBilling
from ..git_host import (ISSUE_CLAIM_MESSAGE, RE_CLAIM, assignment_facts, gh_pages, gh_read,
                        own_issue_claim, recheck_candidate)
from ..job_router import JobRouter, RouterConfig
from ..repo_policy import PolicyEngine
from ..schemas import Job, Result
from ..roster import monitored_repo
from ..state_store import ClaimLedger, Worklog, queue_transaction, business_day, business_now, atomic_json_write
from ..state_store import publication_quota_remaining, candidate_fifo_key, candidate_execution_lock
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
    with queue_transaction(root) as q:
        if key in q:
            q[key]["status"] = status
            q[key]["updated_at"] = business_now().isoformat(timespec="seconds")
            if extra:
                q[key].update(extra)


def _emergency_release(root: Path, candidate_id: str = "", tz_name: str = "Asia/Shanghai") -> None:
    """Never silently rerun an interrupted remote write."""
    if candidate_id:
        with queue_transaction(root) as queue:
            candidate = queue.get(candidate_id)
            if candidate and (candidate.get("status") == "running" or (
                    candidate.get("status") == "staged" and candidate.get("dispatch_started_at"))):
                candidate.update(status="escalated", updated_at=business_now(tz_name).isoformat(timespec="seconds"),
                                 abort_reason="controller interrupted; inspect remote job before retry")


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


def _publication_run_missing(runner_repo: str, remote_id: str) -> bool:
    """Only an explicit HTTP 404 is evidence of a missing run, never a timeout."""
    try:
        response = subprocess.run(
            ["gh", "api", f"repos/{runner_repo}/actions/runs/{remote_id}", "--include"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return response.returncode != 0 and bool(
        re.search(r"^HTTP/\S+ 404\b", response.stdout, re.MULTILINE))


def _find_repair_run(runner_repo: str, job_id: str) -> tuple[dict | None, bool]:
    """Find an exact job marker; only a complete listing proves a run absent."""
    page, seen = 1, set()
    while True:
        response = gh_read([f"repos/{runner_repo}/actions/workflows/oss-scout-repair.yml/runs"
                            f"?event=workflow_dispatch&per_page=100&page={page}"])
        if not isinstance(response, dict) or not isinstance(response.get("workflow_runs"), list):
            return None, False
        runs = response["workflow_runs"]
        match = next((r for r in runs if r.get("display_title") == f"repair {job_id}"), None)
        if match:
            return match, True
        if len(runs) < 100:
            return None, True
        ids = {r.get("id") for r in runs}
        if ids <= seen:
            return None, False
        seen.update(ids)
        page += 1


def _retryable_codex_communication(details: list) -> bool:
    from .codex_client import CODEX_COMMUNICATION_RETRY_PREFIX
    # An older worker may finish after the controller has been upgraded.
    legacy = r"Codex start/result error: (?:TimeoutError|URLError)\b|Codex login broker returned (?:408|429|5\d\d)\b"
    return any(CODEX_COMMUNICATION_RETRY_PREFIX in str(detail) or re.search(legacy, str(detail))
               for detail in details)


def _authorized_retry(entry: dict) -> bool:
    return bool(entry.get("retry_authorized_job_id")
                and entry.get("retry_authorized_job_id") == entry.get("repair_job_id")
                and not entry.get("pr_url"))


def _communication_retry_fields(entry: dict, job_id: str, reason: str, tz_name: str) -> dict:
    # Reconciliation can repeat after a restart; do not extend backoff each time.
    if entry.get("last_communication_job_id") == job_id:
        return {}
    now = business_now(tz_name)
    failures = int(entry.get("communication_failures") or 0) + 1
    delay = min(60, 5 * 2 ** min(failures - 1, 4))
    return {"repair_outcome": "communication_unavailable", "abort_reason": reason[:300],
            "communication_failures": failures, "last_communication_job_id": job_id,
            "retry_authorized_job_id": job_id,
            "screen_retry_at": (now + dt.timedelta(minutes=delay)).isoformat(),
            "eligible_at": (now + dt.timedelta(minutes=delay)).isoformat(),
            "scheduled_consume_day": now.date().isoformat(),
            "staged_at": entry.get("staged_at") or now.isoformat()}


def _reconcile_publication(ledger: ClaimLedger, tz_name: str, *, root: Path | None = None,
                           worker_result: dict | None = None, job_id: str | None = None) -> dict | None:
    """Resolve one writer; callers own its per-candidate execution lock."""
    pending = ledger.pending_publication(job_id)
    if pending is None:
        return {}
    job_id, repo, head, runner_repo, remote_id, expected_url = pending
    if not remote_id:
        run, _ = _find_repair_run(runner_repo, job_id)
        if run is None:
            return None
        remote_id = str(run["id"])
        ledger.set_publication_run(job_id, remote_id)
    run = gh_read([f"repos/{runner_repo}/actions/runs/{remote_id}"])
    missing_run = False
    if run is None and _publication_run_missing(runner_repo, str(remote_id)):
        # A 404 can also hide a repository permission failure. Require a
        # successful run listing, and recover a replacement ID before concluding
        # that this writer no longer exists.
        run, complete = _find_repair_run(runner_repo, job_id)
        if run is None and not complete:
            return None
        if run is not None:
            remote_id = str(run["id"])
            ledger.set_publication_run(job_id, remote_id)
        else:
            missing_run = True
            run = {"display_title": f"repair {job_id}", "status": "completed",
                   "conclusion": "unavailable"}
    if not run or run.get("display_title") != f"repair {job_id}" or run.get("status") != "completed":
        return None
    prs = gh_read([f"repos/{repo}/pulls", "-X", "GET", "-f", "state=all", "-f", f"head={head}"])
    if not isinstance(prs, list):
        return None
    pr = next((p for p in prs if (p.get("user") or {}).get("login") == FORK_OWNER
               and (p.get("html_url") == expected_url
                    or f"<!-- oss-scout-job:{job_id} -->" in (p.get("body") or ""))), None)
    if pr is None and expected_url:
        return None
    if missing_run and prs and pr is None:
        # Unexpected head matches need investigation rather than an assumption
        # that no PR was published.
        return None
    created = dt.datetime.fromisoformat(pr["created_at"].replace("Z", "+00:00")) if pr else None
    issue = re.fullmatch(r"[^:]+:fix/issue-(\d+)-[0-9a-f]+(?:-[0-9a-f]+)?", head)
    if root is not None and issue:
        recovery = worker_result is None
        communication_retry = False
        key = f"{repo}#{issue[1]}"
        if pr:
            status = "pr_open"
            extra = {"pr_url": pr["html_url"], "repair_outcome": "succeeded", "abort_reason": ""}
        else:
            result = worker_result
            if recovery and not missing_run:
                result = ActionsBackend(runner_repo=runner_repo, artifact_dir=root / "artifacts").collect(str(remote_id))
            result = result or {}
            if result.get("job_id") != job_id:
                if (run.get("conclusion") == "success"
                        and "no artifacts and logs unavailable" in (result.get("unverified_items") or [])):
                    return None
                result = {}
            publication = result.get("publish") or {}
            if publication.get("created") and publication.get("pr"):
                ledger.expect_publication(job_id, publication["pr"])
                return None
            outcome = result.get("outcome")
            details = result.get("unverified_items") or []
            communication_retry = outcome != "succeeded" and _retryable_codex_communication(details)
            if communication_retry:
                status = "staged"
                reason = f"临时通信故障，暂存重试 · {details[0]}"
                # The remote run is complete and its head has no PR. Keep the
                # attempt history, but authorize a fresh claim after backoff.
                ledger.release_claim(key, tz_name)
            elif outcome and outcome != "succeeded":
                status = "aborted_no_evidence"
                reason = f"修复结果 {outcome}，未发布 PR"
                if details:
                    reason += f" · {details[0]}"
            elif outcome == "succeeded":
                reason = "修复完成，但未发布 PR"
                if publication.get("error"):
                    reason += f" · {publication['error']}"
                status = "aborted_pipeline"
            elif run.get("conclusion") in ("failure", "cancelled", "timed_out"):
                status, reason = "aborted_pipeline", f"工作流已结束（{run['conclusion']}），未发布 PR"
            elif missing_run:
                status, reason = "escalated", "Actions 运行记录已不存在；已核对目标分支未开 PR，转人工核查"
            else:
                status, reason = "escalated", "工作流已结束，未发布 PR，修复结果无法确认"
            extra = {"repair_outcome": "communication_unavailable" if communication_retry else outcome or "unknown",
                     "abort_reason": reason[:300]}
            if publication.get("coarse_reason"):
                status = "coarse_blocked"
                extra.update(coarse_reason=publication["coarse_reason"],
                             abort_reason=publication["coarse_reason"])
        # Save the candidate first. If the controller stops here, the pending
        # publication still exists and the same result can safely be reconciled.
        with queue_transaction(root) as queue:
            actual_key = next((k for k in queue if k.lower() == key.lower()), key)
            candidate = queue.get(actual_key)
            if candidate is not None:
                if communication_retry:
                    extra.update(_communication_retry_fields(candidate, job_id, reason, tz_name))
                candidate.update(status=status, updated_at=business_now().isoformat(timespec="seconds"),
                                 repair_job_id=job_id, repair_run_id=str(remote_id),
                                 workflow_conclusion=run.get("conclusion"), **extra)
                candidate.pop("dispatch_started_at", None)
                candidate.pop("dispatch_job_id", None)
                candidate.pop("direct_start_reserved_at", None)
        if recovery:
            if pr:
                log(root, "pr_open", key, f"{pr['html_url']} (reconciled from Actions)")
            else:
                log(root, "reconciled", key, f"{status} · {extra['abort_reason']}")
    if pr:
        ledger.finish_publication(job_id, day=business_day(created, tz_name), url=pr["html_url"])
    else:
        ledger.finish_publication(job_id)
    if root is not None:
        try:
            (root / "artifacts" / "issue-context" / f"{job_id}.json").unlink(missing_ok=True)
        except OSError:
            pass
    return pr or {}


def recover_pending_publication(root: Path, tz_name: str = "Asia/Shanghai") -> bool:
    """Reconcile every orphaned writer without blocking live, unrelated jobs."""
    root = _data_root(root)
    ledger = ClaimLedger(root / "state/scout_v2.sqlite3")
    changed = False
    try:
        for pending in ledger.pending_publications():
            job_id, repo, head, _, _, _ = pending
            match = re.search(r":fix/issue-(\d+)-", head)
            candidate = f"{repo}#{match[1]}" if match else job_id
            with candidate_execution_lock(root, candidate) as owned:
                if not owned:
                    continue
                try:
                    receipt = _reconcile_publication(ledger, tz_name, root=root, job_id=job_id)
                    changed = receipt is not None or changed
                except Exception as exc:
                    log(root, "reconcile_error", job_id, str(exc)[:180])
        return changed
    finally:
        ledger.close()


def refresh_publication_status(root: Path, tz_name: str = "Asia/Shanghai") -> bool:
    """Check GitHub for changes to today's confirmed publications."""
    day = business_now(tz_name).date().isoformat()
    ledger = ClaimLedger(root / "state/scout_v2.sqlite3")
    changed = False
    try:
        for repo, url in ledger.publications_to_check(day):
            number = url.rsplit("/", 1)[-1]
            if not number.isascii() or not number.isdecimal() or not monitored_repo(repo):
                continue
            pr = gh_read([f"repos/{repo}/pulls/{number}"], max_retries=0)
            if not isinstance(pr, dict) or str(pr.get("html_url") or "").lower() != url.lower():
                continue
            closed_day = None
            if pr.get("merged") is True:
                state = "merged"
            elif pr.get("merged") is False and pr.get("state") == "closed" and pr.get("closed_at"):
                try:
                    closed_day = business_day(dt.datetime.fromisoformat(pr["closed_at"]), tz_name)
                except (ValueError, TypeError):
                    continue
                state = "closed"
            elif pr.get("merged") is False and pr.get("state") == "open":
                state = "open"
            else:
                continue
            if ledger.record_publication_status(url, state, closed_day):
                changed = True
                log(root, "publication_status", url, f"{state}; closed_day={closed_day or '-'}")
    finally:
        ledger.close()
    return changed


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


# Modal 通信不走代理的逻辑已内建进 ModalSandboxBackend（no_proxy_context）：
# submit/poll/collect 内部自己包裹，调用方无需再包。这里保留名字作别名，
# scripts/diag_proxy2.py 诊断脚本引用了它。
from ..adapters.modal_backend import no_proxy_context as _no_proxy_context  # noqa: E402,F401


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
        # Modal 后端内部已处理无代理（no_proxy_context），无需包裹
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
        st = be.poll(remote_id)
        state = st.get("state")
        if extra.get("candidate_id") and st.get("started_at"):
            with queue_transaction(root) as queue:
                candidate = queue.get(extra["candidate_id"])
                if candidate and candidate.get("status") == "staged":
                    candidate.update(status="running", started_at=st["started_at"],
                                     updated_at=business_now().isoformat(timespec="seconds"))
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

def _todays_claim_count(root: Path, key: str) -> int:
    """worklog 里今天这条候选的认领次数（并发安全的保险丝计数）。"""
    today = business_now().date().isoformat()
    n = 0
    try:
        wl = (root / "state" / "worklog.jsonl").read_text(
            encoding="utf-8", errors="replace")
        for line in wl.splitlines():
            if f'"target": "{key}"' not in line or '"claim"' not in line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (str(rec.get("action")) == "claim"
                    and str(rec.get("target")) == key
                    and str(rec.get("ts", "")).startswith(today)):
                n += 1
    except OSError:
        return 0
    return n


def _ensure_issue_claim(repo: str, number: int) -> str:
    """Recognize an earlier claim before writing, including after a lost response."""
    issue_path = f"repos/{repo}/issues/{number}"
    issue = gh_read([issue_path])
    if not isinstance(issue, dict) or not isinstance(issue.get("assignees"), list):
        return "unavailable"
    if issue.get("state") != "open" or issue.get("pull_request") or assignment_facts(issue, {})["assigned_to_others"]:
        return "blocked"
    path = f"{issue_path}/comments"
    comments = gh_pages(path, 100)
    if comments is None:
        return "unavailable"
    own = [own_issue_claim(comment, FORK_OWNER) for comment in comments]
    from ..contribution_intent import claim_evidence
    if any(not mine and claim_evidence(str(comment.get("body") or ""), pattern=RE_CLAIM)
           for comment, mine in zip(comments, own)):
        return "blocked"
    if any(own):
        return "confirmed"
    try:
        result = subprocess.run(
            ["gh", "api", "-X", "POST", path, "-f", f"body={ISSUE_CLAIM_MESSAGE}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        if result.returncode:
            return "rejected" if re.search(r"HTTP (?:401|403|404|422)\b", result.stderr) else "unavailable"
        return "confirmed" if own_issue_claim(json.loads(result.stdout), FORK_OWNER) else "rejected"
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return "unavailable"


def _abort_release_impl(msg: str, *, candidate_id: str, ledger, eng,
                   summary: dict, log_fn, root: Path, key: str,
                   give_back: bool = True) -> dict:
    """Record failed attempts without charging the publication budget."""
    if ledger is not None:
        ledger.close()
        with queue_transaction(root) as q:
            if key in q and (q[key].get("status") == "running" or (
                    q[key].get("status") == "staged" and q[key].get("dispatch_started_at"))):
                q[key].update(status="aborted_pipeline", abort_reason=msg[:300])
                q[key].pop("dispatch_started_at", None)
    summary["abort"] = msg
    log_fn(root, "abort", key, msg)
    return summary


def candidate_pipeline(root: Path, entry: dict, *, max_files: int = 6, rescreen: bool = False,
                       immediate: bool = False) -> dict:
    """Reserve budget before preparation; serialize only the same candidate."""
    import sqlite3
    import uuid
    root = _data_root(root)
    (root / "state").mkdir(parents=True, exist_ok=True)
    key = str(entry.get("id") or "")
    admission = sqlite3.connect(root / "state/dispatcher.sqlite3", timeout=0, isolation_level=None)
    try:
        try:
            admission.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower():
                return {"id": key, "abort": "dispatcher busy"}
            raise
        with candidate_execution_lock(root, key) as owned:
            if not owned:
                return {"id": key, "abort": "dispatcher busy"}
            eng, err = PolicyEngine.load(root)
            if err:
                return {"id": key, "abort": f"policy fail-closed: {err}"}
            ledger = ClaimLedger(root / "state/scout_v2.sqlite3")
            job_id = f"job-{uuid.uuid4()}"
            reserved = False
            try:
                with queue_transaction(root) as queue:
                    latest = queue.get(key)
                    if latest is not None and latest.get("status") != "staged":
                        return {"id": key, "abort": "candidate already running or terminal"}
                    work = {**entry, **(latest or {})}
                    repo = work.get("repo") or key.rsplit("#", 1)[0]
                    backed = {candidate.lower() for candidate in ledger.reserved_candidates()}
                    unbacked = sum(k != key and k.lower() not in backed
                                   and e.get("status") in ("staged", "running")
                                   and bool(e.get("direct_start_reserved_at") or e.get("dispatch_started_at")
                                            or e.get("status") == "running") for k, e in queue.items())
                    if ledger.dispatch_capacity(eng.tz_name, eng.max_claims_per_day)["available"] <= unbacked:
                        return {"id": key, "abort": "dispatcher busy"}
                    if not ledger.reserve_dispatch(eng.tz_name, eng.max_claims_per_day, job_id, repo, key):
                        if latest is not None and key.lower() not in {k.lower() for k in ledger.reserved_candidates()}:
                            latest.pop("direct_start_reserved_at", None)
                            from ..state_store import rotate_staged_queue
                            for old in rotate_staged_queue(root, queue):
                                log(root, "queue_evicted", old, "handoff had no available execution slot")
                        return {"id": key, "abort": "dispatcher busy"}
                    reserved = True
                    work["dispatch_job_id"] = job_id
                    if latest is not None:
                        latest["dispatch_job_id"] = job_id
                        latest.setdefault("direct_start_reserved_at", business_now().isoformat())
                        work = {**work, **latest}
                        work.setdefault("id", key)
                admission.close()
                admission = None
                try:
                    return _candidate_pipeline(root, work, max_files=max_files, rescreen=rescreen, immediate=immediate)
                except Exception as exc:
                    _emergency_release(root, key)
                    log(root, "crash", key, str(exc)[:200])
                    raise
            finally:
                ledger.release_preparation(job_id)
                pending = ledger.pending_publication(job_id)
                if reserved and pending is None:
                    with queue_transaction(root) as queue:
                        if queue.get(key, {}).get("dispatch_job_id") == job_id:
                            queue[key].pop("dispatch_job_id", None)
                    release_direct_start(root, key)
                ledger.close()
    finally:
        if admission is not None:
            admission.close()


def release_direct_start(root: Path, key: str) -> None:
    """A failed handoff either joins the bounded waiters or records a clear result."""
    with queue_transaction(root) as queue:
        entry = queue.get(key)
        if not entry or not entry.pop("direct_start_reserved_at", None):
            return
        if entry.get("status") == "staged" and not entry.get("dispatch_started_at"):
            from scout_v2.state_store import rotate_staged_queue
            for old in rotate_staged_queue(root, queue, keep=(key,), replacement=key):
                log(root, "queue_evicted", old, f"replaced by {key} after direct handoff")


def expire_direct_starts(root: Path) -> None:
    """An unstarted handoff cannot reserve today's slot after a day rollover."""
    import sqlite3
    (root / "state").mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(root / "state/dispatcher.sqlite3"), timeout=0, isolation_level=None)
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower():
                return  # a pipeline owns the slot; never touch that reservation
            raise
        path = root / "queue/candidates.json"
        queue = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        now = business_now()
        ledger = ClaimLedger(root / "state/scout_v2.sqlite3")
        try:
            protected = {key.lower() for key in ledger.reserved_candidates()}
        finally:
            ledger.close()
        for key, entry in queue.items():
            if key.lower() in protected:
                continue
            stamp = entry.get("direct_start_reserved_at")
            if not stamp or entry.get("status") != "staged" or entry.get("dispatch_started_at"):
                continue
            if business_day(dt.datetime.fromisoformat(stamp), "Asia/Shanghai") != now.date().isoformat():
                release_direct_start(root, key)
    finally:
        conn.close()


def _candidate_pipeline(root: Path, entry: dict, *, max_files: int = 6, rescreen: bool = False,
                        immediate: bool = False) -> dict:
    """单候选：复检 → 合规 → 后端修复 → 质量闸 → push + PR。"""
    root = _data_root(root)
    repo, number = entry["repo"], entry["number"]
    dispatch_job_id = entry.get("dispatch_job_id")
    key = entry["id"]
    summary: dict = {"id": key, "steps": []}
    ledger = None
    qpath = root / "queue/candidates.json"
    latest = None
    if qpath.exists():
        latest = json.loads(qpath.read_text(encoding="utf-8")).get(key)
        if latest is not None:
            immediate = immediate and latest == entry
            entry = dict(latest)
            if entry.get("status") != "staged":
                return {**summary, "abort": "candidate not currently eligible"}

    rescreen = rescreen or _fine_rescreen_due(entry)

    def _abort_release(msg: str, *, give_back: bool = True) -> dict:
        # 委托模块级实现（显式传参）。2026-11-06 实录：模块级版本曾漏参
        # 直接暴露，每次 abort NameError，competed 候选崩溃循环。
        return _abort_release_impl(
            msg, candidate_id=key, ledger=ledger, eng=eng, summary=summary,
            log_fn=log, root=root, key=key, give_back=give_back)

    eng, err = PolicyEngine.load(root)
    if err is not None:
        summary["abort"] = f"policy fail-closed: {err}"
        return summary
    # Authentication outages are retryable infrastructure failures, not bad repairs.
    from scout_v2.console_models import repair_settings
    from scout_v2.codex_auth import auth_ready
    try:
        repair_agent, _ = repair_settings(root)
    except Exception:
        repair_agent = None  # The existing model validation below reports invalid settings.
    if repair_agent == "codex" and not auth_ready(root):
        reason = "Codex login temporarily unavailable; waiting for credential recovery"
        mark_candidate(root, key, "staged", extra={
            "abort_reason": reason, "rescreen_pending": rescreen,
            "screen_retry_at": (business_now(eng.tz_name) + dt.timedelta(minutes=5)).isoformat()})
        return {**summary, "abort": reason}
    lane = eng.repo_lane(repo)
    assignment_rule = eng.pr_assignment(repo) if hasattr(eng, "pr_assignment") else {}
    wl = Worklog(root / "state" / "worklog.jsonl")

    if not monitored_repo(repo) or lane in ("unknown", "observe"):
        summary["abort"] = f"lane={lane} no action"
        # 必须写终态，否则状态留在 new，drain 每轮都会重新取到它。
        # 2026-10-30 实测：修复循环改成 60 秒后，旧名单遗留的
        # anomalyco/opencode、deepset-ai/haystack 候选每分钟被重试一次，
        # 每次都 lane=unknown 空转。
        reason = eng.repo_entry(repo).get("review_reason") or "仓库不在自动修复范围"
        mark_candidate(root, key, "coarse_blocked", extra={"coarse_reason": reason})
        return summary
    budget = ClaimLedger(root / "state/scout_v2.sqlite3")
    try:
        day = business_now(eng.tz_name).date().isoformat()
        refill_day = day if budget.has_publication_release(day) else ""
        if latest is not None and not _eligible(entry, refill_day=refill_day):
            return {**summary, "abort": "candidate not currently eligible"}
        if budget.quota_used(day) >= eng.max_claims_per_day:
            _stage_candidates(root, [key], tz_name=eng.tz_name, cap=eng.max_claims_per_day)
            return {**summary, "abort": "daily PR limit reached"}
    finally:
        budget.close()
    runner_repo = _actions_runner_repo(root)
    workflow = gh_read([f"repos/{runner_repo}/actions/workflows/{_actions_workflow_name(root)}"])
    if not workflow or workflow.get("state") != "active":
        return {**summary, "abort": "repair workflow unavailable; candidate retained"}
    summary["steps"].append(f"lane={lane}")

    # Only the synchronous intake handoff can reuse its live coarse check.
    reuse_screen = immediate and latest is not None and entry.get("status") == "staged" and not rescreen
    recheck = None
    fresh = None
    from dashboard.data import (coarse_issue_blockers, fine_screen_issue,
                                issue_screening_snapshot, live_issue_blockers)

    if reuse_screen:
        summary["steps"].append("reused live intake screening")
    else:
        recheck = recheck_candidate(repo, number, use_title_keywords=True)
        if recheck is None or not recheck.complete:
            summary["abort"] = f"recheck incomplete: {recheck.errors if recheck else 'None'}"
            log(root, "abort", key, summary["abort"])
            # 粗筛取数失败仍保留暂存，按独立退避时间重试。修复循环
            # 现在每 60 秒一轮，如果 GitHub 持续抖动就会每分钟打一次。记失败次数，
            # 超过 3 次就标 escalated 不再自动重试，避免无意义刷 API。
            n = int(entry.get("recheck_failures") or 0) + 1
            mark_candidate(root, key, "escalated" if n >= 3 else "staged",
                           extra={"recheck_failures": n, "rescreen_pending": rescreen,
                                  "abort_reason": summary["abort"],
                                  "screen_retry_at": (business_now() + dt.timedelta(minutes=5)).isoformat()})
            if n >= 3:
                from ..notify import feishu_send
                feishu_send(f"⚠️ 候选升级转人工：{key}",
                            f"复检连续失败 {n} 次：{summary.get('abort', '')[:150]}")
            return summary
        if getattr(recheck, "similar_prs", []):
            log(root, "similar_pr", key, "；".join(recheck.similar_prs))
        source = getattr(recheck, "screening_source", None)
        if source:
            issue, comments, timeline = source
            fresh = issue_screening_snapshot(entry, issue, comments, timeline)
        try:
            blockers = coarse_issue_blockers(
                eng, fresh or entry, lane=lane, live_blockers=live_issue_blockers(recheck), root=root)
        except Exception as ex:
            summary["abort"] = f"coarse screening unavailable: {ex}"
            mark_candidate(root, key, "staged", extra={
                "screen_retry_at": (business_now() + dt.timedelta(minutes=5)).isoformat(),
                "abort_reason": summary["abort"], "rescreen_pending": rescreen})
            return summary
        reason = ", ".join(blockers)
        log(root, "coarse_screen", key, reason or "通过机械条件")
        if blockers:
            mark_candidate(root, key, "coarse_blocked",
                           extra={"blockers": blockers, "coarse_reason": reason, "abort_reason": reason})
            return _abort_release("coarse screening blocked: " + reason)
        summary["steps"].append("recheck ok")

    # Fresh automatic dispatch reuses the inbox verdict. Delayed or manual
    # dispatch evaluates the current evidence again before claiming work.
    approved = (entry.get("ai_verdict") or {}).get("usable")
    if approved is None:
        approved = (entry.get("ai_grade") or {}).get("grade") == "A"
    if approved is not True:
        mark_candidate(root, key, "escalated", extra={"abort_reason": "no approved screening verdict"})
        return _abort_release("candidate has no approved screening verdict")
    if not rescreen and getattr(recheck, "issue_title", None) is not None:
        if ((entry.get("ai_verdict") or {}).get("provider") == "codiv"
                and (recheck.issue_title != entry.get("title") or recheck.issue_body != entry.get("body_excerpt"))):
            mark_candidate(root, key, "escalated", extra={"abort_reason": "issue title or body changed after screening"})
            return _abort_release("issue title or body changed after screening")
        entry = {**entry, "title": recheck.issue_title, "body_excerpt": recheck.issue_body}

    # ---- 认领闸 ----
    # 同一条候选今天的尝试次数上限：2026-11-03 实测 deer-flow#7846 在一天内被
    # claim 7 次（每次 abort 都归还额度），同一份活反复烧 Actions 分钟。额度闸
    # 管的是"总共几次机会"，这里管的是"一条候选最多试几次"。
    #
    # 2026-11-05：计数来源从 candidates.json 的 attempts 字段改为 worklog 里
    # 今天的 claim 记录——candidates.json 由 ingest 即时派发/监控循环/drain
    # 三个线程并发原子重写，attempts 计数被互相覆盖丢失（fastmcp#6298 一天
    # 被认领 6 次、istio#61045 每分钟重试，保险丝永远读不到 3）。worklog 是
    # 只追加的审计账本，认领动作必然留痕，按条数计数并发安全。
    attempts = _todays_claim_count(root, key)
    retry_authorized = _authorized_retry(entry)
    if attempts >= MAX_ATTEMPTS_PER_CANDIDATE and not retry_authorized:
        summary["abort"] = (f"candidate attempt limit reached "
                            f"({attempts}/{MAX_ATTEMPTS_PER_CANDIDATE})")
        log(root, "abort", key, summary["abort"])
        mark_candidate(root, key, "escalated",
                       extra={"abort_reason": summary["abort"]})
        from ..notify import feishu_send
        feishu_send(f"⚠️ 候选尝试超限转人工：{key}", summary["abort"][:150])
        return summary

    # Recheck and CLA failures have not started a repair and spend no quota.
    if rescreen:
        verdict = (fine_screen_issue(eng, fresh, lane=lane) if fresh is not None else
                   {"usable": None, "reason": "current screening evidence unavailable"})
        if verdict.get("skipped"):
            reason = verdict["reason"]
            mark_candidate(root, key, "screen_skipped", extra={
                "abort_reason": reason, "fine_reason": reason,
                "rescreen_pending": False, "screen_retry_at": None})
            return _abort_release(f"queue fine screening skipped: {reason}")
        if verdict.get("usable") is not True:
            retry = verdict.get("usable") is None
            status = "staged" if retry else "fine_blocked"
            reason = str(verdict.get("reason") or "no eligibility verdict")
            mark_candidate(root, key, status, extra={"abort_reason": reason, "fine_reason": reason, "rescreen_pending": retry,
                "screen_retry_at": (business_now() + dt.timedelta(minutes=5)).isoformat() if retry else None})
            return _abort_release(f"queue fine screening {'unavailable' if retry else 'rejected'}: {reason}")
        entry = {**fresh, "ai_verdict": verdict, "screened_at": business_now().isoformat(),
                 "rescreen_pending": False, "fine_reason": verdict.get("reason", "")}
        mark_candidate(root, key, "staged",
                       extra={k: v for k, v in entry.items() if k != "updated_at"})
        summary["steps"].append("queue coarse and fine screening passed")
    if assignment_rule.get("claim_comment"):
        claim = _ensure_issue_claim(repo, number)
        if claim != "confirmed":
            reason = "仓库要求先认领 · " + {
                "blocked": "Issue 已关闭、已被指派或已有认领",
                "rejected": "GitHub 拒绝认领留言",
                "unavailable": "暂无法确认认领留言",
            }[claim]
            status = "staged" if claim == "unavailable" else "coarse_blocked"
            mark_candidate(root, key, status,
                           extra={"coarse_reason": reason, "abort_reason": reason,
                                  "screen_retry_at": (business_now() + dt.timedelta(minutes=5)).isoformat()})
            return _abort_release(reason)
        log(root, "issue_claim_confirmed", key, "required upstream claim comment exists")
        summary["steps"].append("upstream claim confirmed")
    ledger = ClaimLedger(root / "state/scout_v2.sqlite3")
    # The queue lock protects state across days; the ledger serializes the cap.
    with queue_transaction(root) as q:
        current = q.get(key)
        if current is not None and current.get("status") != "staged":
            ledger.close()
            return {**summary, "abort": "candidate already running or terminal"}
        if current is not None and not _order_candidates([(key, current)], refill_day=refill_day):
            ledger.close()
            return {**summary, "abort": "candidate not eligible yet"}
        retry_authorized = _authorized_retry(current or entry)
        ok, reason = ledger.try_claim(eng.tz_name, key, retry_failed=retry_authorized)
        if not ok:
            if current is not None and "daily limit" in reason:
                _stage_entry(current, root, q)
            elif current is not None and "already claimed" in reason:
                current.update(status="escalated", abort_reason=reason)
            summary["abort"] = f"claim budget: {reason}"
            ledger.close()
            return summary
        if current is not None:
            current.pop("retry_authorized_job_id", None)  # One dispatch consumes the authorization.
            current.update(attempts=attempts + 1,
                           dispatch_started_at=business_now(eng.tz_name).isoformat())
    wl.append({"ts": business_now(eng.tz_name).isoformat(timespec="seconds"), "action": "claim",
               "target": key, "source": "auto-pipeline"})
    summary["steps"].append("repair attempt recorded")

    # ---- base SHA ----
    head = None
    for attempt in range(3):
        head = subprocess.run(
            ["gh", "api", f"repos/{repo}/commits/{assignment_rule.get('base_branch', 'HEAD')}", "--jq", ".sha"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        if head.returncode == 0:
            break
        time.sleep(min(30, (attempt + 1) * 10))
    if head is None or head.returncode != 0:
        return _abort_release(
            f"head sha failed: {(head.stderr if head else '')[:120]}",
            give_back=True)   # GitHub 抖动，不是机会用掉了
    base_sha = head.stdout.strip()

    # ---- 组 job 并交给后端（VPS 不 clone）----
    out_dir = root / "artifacts" / f"pipe-{key.replace('/', '_').replace('#', '_')}-{int(time.time())}"
    job = Job(mode="repair", repo=repo, base_sha=base_sha,
              idempotency_key=f"repair:{repo}:{base_sha}:{number}",
              execution_scope="project_contribution",
              policy_snapshot_id=eng.snapshot_id(),
              max_estimated_compute_usd=0.5)
    if dispatch_job_id:
        job.job_id = dispatch_job_id
    # 测试框架由 agent 现场判断（2026-10-28）：控制器不再预设 pytest。
    # 之前在这里硬编码探测，"探测不到就放弃"，等于把能力锁死在 Python+pytest。
    # 现在只把 issue 交给 agent，它自己看 Cargo.toml / package.json / 测试目录。
    summary["steps"].append("agent decides test framework (no preset)")
    # 模型名由控制器解析后随 job 下发：worker 镜像里没有 configs/，
    # 无法自己读 model_policy.yaml。
    try:
        from scout_v2.console_models import repair_settings
        job.repair_agent, job.model = repair_settings(root)
    except Exception as e:  # noqa: BLE001
        return _abort_release(f"no model resolved: {str(e)[:160]}",
                             give_back=True)   # 配置/凭证问题
    # 路由器会选后端；只有 Actions 能自己写回（runner 有稳定出网 + PAT），
    # Modal 产出的补丁仍由控制器发布。这里先按路由结论决定要不要带 publish 配置。
    try:
        decision = _build_router(root, job, eng).route(job.to_dict())
    except Exception as e:  # noqa: BLE001
        return _abort_release(f"routing failed: {str(e)[:160]}",
                             give_back=True)   # 路由层异常
    if decision.backend != "github_actions":
        return _abort_release("no GitHub Actions repair backend")
    job.publish = _build_publish_cfg(repo, number, base_sha, entry)
    if assignment_rule.get("base_branch"):
        job.publish["base_branch"] = assignment_rule["base_branch"]
    if assignment_rule.get("requires_assignment"):
        job.publish["assignment_rule"] = assignment_rule
    if assignment_rule.get("commit_issue_reference"):
        job.publish["commit_issue_reference"] = True
    job.publish["branch"] += f"-{job.job_id.rsplit('-', 1)[-1][:8]}"
    job.publish["body"] += f"\n\n<!-- oss-scout-job:{job.job_id} -->"
    head = f"{FORK_OWNER}:{job.publish['branch']}"
    context = fresh if fresh is not None else entry
    evidence = {k: context.get(k) for k in (
        "title", "body_excerpt", "reporter", "reporter_association", "labels",
        "assignees", "issue_comments", "related_prs")}
    evidence["url"] = f"https://github.com/{repo}/issues/{number}"
    context_path = root / "artifacts" / "issue-context" / f"{job.job_id}.json"
    try:
        from ..state_store import atomic_json_write
        context_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(context_path, evidence)
        job.context = f"oss-scout-snapshot:{job.job_id}"
    except OSError as e:
        return _abort_release(f"issue snapshot unavailable: {str(e)[:150]}", give_back=True)
    if not ledger.reserve_publication(eng.tz_name, eng.max_claims_per_day,
                                      job.job_id, repo, head, runner_repo):
        context_path.unlink(missing_ok=True)
        return _abort_release("publication slot unavailable")
    try:
        backend, remote_id, extra = _submit_job(root, job, eng)
    except Exception as e:  # noqa: BLE001
        if re.search(r"workflow_dispatch failed:.*(?:HTTP (?:403|404|422)|disabled workflow)", str(e), re.I):
            ledger.finish_publication(job.job_id)
            context_path.unlink(missing_ok=True)
        return _abort_release(f"backend unavailable: {str(e)[:200]}",
                             give_back=True)   # 后端故障
    summary["steps"].append(f"backend={backend} ({extra.get('note', '')})")

    ledger.set_publication_run(job.job_id, remote_id)
    extra["candidate_id"] = key
    collected = _collect(root, backend, remote_id, extra)
    runner_pr = _runner_published(collected)
    if runner_pr and runner_pr.get("created") and runner_pr.get("pr"):
        ledger.expect_publication(job.job_id, runner_pr["pr"])
    receipt = _reconcile_publication(
        ledger, eng.tz_name, root=root,
        worker_result={**(collected.get("result") or {}), "publish": runner_pr}, job_id=job.job_id,
    )
    if receipt is None:
        return _abort_release("publication outcome unconfirmed; awaiting reconciliation")
    res = Result.from_dict(collected["result"]) if isinstance(collected.get("result"), dict) \
        else Result(job_id=job.job_id, backend=backend, outcome="failed",
                    base_sha=base_sha, unverified_items=["no result from backend"])
    if res.backend in ("", None):
        res.backend = backend
    if not receipt and _retryable_codex_communication(res.unverified_items):
        ledger.close()
        summary.update(abort="temporary Codex communication failure; staged for retry", retry=True)
        log(root, "communication_retry", key, res.unverified_items[0][:300])
        return summary
    patch_file = _stage_patch_from_backend(root, out_dir, collected)

    # ---- 质量闸 ----
    report = verify_result(res, job, artifact_dir=root)
    if not (res.outcome == "succeeded" and res.reproduced_on_base
            and res.fixed_on_patch and report.ok and patch_file) and not receipt:
        _r = _abort_release(
            f"evidence chain incomplete: {res.outcome} repro={res.reproduced_on_base} "
            f"fixed={res.fixed_on_patch} gate={report.errors or 'PASS'} "
            f"patch={'yes' if patch_file else 'no'}",
            give_back=True)   # 无证据也算尝试失败：退坑让下一条候选顶上
        _r["repair"] = {"backend": backend, "outcome": res.outcome,
                        "repro": res.reproduced_on_base, "fixed": res.fixed_on_patch,
                        "unverified": res.unverified_items[:3]}
        # 原因必须落到候选条目上：只写 worklog 的话，事后统计（如 2026-10-31
        # 排查 66 条 aborted_no_evidence）得去解析一整个 JSONL 才能拿到分布。
        mark_candidate(root, key, "aborted_no_evidence",
                       extra={"abort_reason": (res.unverified_items[0] if res.unverified_items else _r["abort"])[:300],
                              "repair_outcome": res.outcome,
                              "repro": bool(res.reproduced_on_base),
                              "fixed": bool(res.fixed_on_patch)})
        return _r
    summary["steps"].append("repair + quality gate ok")

    # ---- 发布：优先用 runner 侧已完成的推送 ----
    # Actions 后端在 payload 带 publish 配置时会自己 push+PR，并把结果放进信封。
    # 这样 VPS 完全不碰 GitHub 写接口，也就没有出网单点。Modal 后端仍由控制器发布。
    if receipt:
        pr_url = receipt["html_url"]
        summary["steps"].append("published on runner")
    elif runner_pr is not None:
        if runner_pr.get("ok"):
            pr_url = runner_pr.get("pr") or ""
            summary["steps"].append("published on runner")
        else:
            summary["abort"] = f"runner publish failed: {runner_pr.get('error', '')[:180]}"
            return _abort_release(summary["abort"], give_back=True)
    else:
        return _abort_release("runner did not publish a PR")
    if not pr_url:
        return _abort_release("publish reported success but no PR url",
                              give_back=True)
    summary["pr"] = pr_url
    log(root, "pr_open", key, f"{pr_url} (auto pipeline, backend={backend})")
    mark_candidate(root, key, "pr_open", extra={"pr_url": pr_url})
    from ..notify import feishu_send
    if receipt:
        feishu_send(f"🚀 新 PR 开出：{key}", f"{job.publish['title']} {pr_url}")

    # PR 开出来之后立刻把能自动签的 CLA 签掉。phoenix 这类仓库的 CLA bot 认
    # 一条固定口令的 issue_comment，发出去就生效；剩下要网页 OAuth 的，preflight
    # 已经在派发前拦住了，走不到这里。
    #
    # 注意用 PR 号而不是 issue 号：CLA bot 认的是评论所在的 PR，两者编号不同。
    try:
        import re as _re
        m = _re.search(r"/pull/(\d+)", pr_url or "")
        pr_no = int(m.group(1)) if m else None
        if pr_no:
            from .cla import sign
            res = sign(root, repo, pr_no)
            if res.get("action") == "signed":
                summary["steps"].append("cla signed")
                log(root, "cla_signed", key, res.get("detail", "")[:100])
            elif not res.get("ok"):
                summary["steps"].append(f"cla: {res.get('action')}")
                log(root, "cla_failed", key, str(res.get("detail"))[:200])
    except Exception as e:  # noqa: BLE001 — 签 CLA 失败不影响已开的 PR
        log(root, "cla_error", key, str(e)[:200])

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
        from .commit_identity import commit_as_owner
        commit_as_owner(clone, f"fix: {str(entry.get('title', ''))[:60]}").check_returncode()
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

def expire_staged(root: Path) -> int:
    expired = []
    with queue_transaction(root) as q:
        for key, entry in q.items():
            if (entry.get("status") == "staged" and not entry.get("dispatch_started_at")
                    and not entry.get("direct_start_reserved_at")
                    and _stage_age_days(entry) > STAGE_TTL_DAYS):
                entry["status"] = "stage_expired"
                expired.append(key)
    if expired:
        log(root, "stage_expired", str(len(expired)), ", ".join(expired[:5]))
    return len(expired)


def _stage_candidates(root: Path, keys: list[str], *, tz_name: str, cap: int) -> int:
    """把候选标成暂存，留到下一轮再处理。返回实际改动的条数。

    用于额度用完之后：ingest 一次扫到 10 条、今天只剩 1 个额度，剩下 9 条不该
    继续躺在 new 里被每轮反复扫描，也不该丢掉——标 staged 并记时间，drain 时
    会优先清暂存。
    """
    if not keys:
        return 0
    n = 0
    with queue_transaction(root) as q:
        if publication_quota_remaining(root, tz_name, cap):
            return 0
        for key in sorted(keys, key=lambda k: ((q.get(k) or {}).get("discovered_at") or "", k)):
            e = q.get(key)
            if (isinstance(e, dict) and e.get("status") == "staged"
                    and not e.get("dispatch_started_at") and not e.get("direct_start_reserved_at")):
                if e.get("status") == "staged" and _stage_age_days(e) > STAGE_TTL_DAYS:
                    e["status"] = "stage_expired"
                    continue
                previous = e.get("eligible_at")
                _stage_entry(e, root, q)
                n += int(previous != e.get("eligible_at"))
    if n:
        log(root, "stage", f"{n} candidates", "next business day at 04:00")
    return n


def _stage_entry(entry: dict, root: Path, queue: dict) -> None:
    _defer_entry(entry, root)
    from scout_v2.state_store import rotate_staged_queue
    for old in rotate_staged_queue(root, queue):
        log(root, "queue_evicted", old, "rolling capacity during quota deferral")


def _defer_entry(entry: dict, root: Path) -> None:
    import datetime as dt
    import yaml
    now = business_now()
    hour, minute = 4, 0
    cfg = root / "configs/dashboard.yaml"
    if cfg.exists():
        schedule = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        hour = int(schedule.get("staged_flush_hour_local", 4))
        minute = int(schedule.get("staged_flush_minute_local", 0))
    entry["status"] = "staged"
    entry["defer_reason"] = "daily_quota"
    entry.setdefault("staged_at", entry.get("discovered_at") or now.isoformat(timespec="seconds"))
    due = entry.get("eligible_at")
    due_time = dt.datetime.fromisoformat(due) if due else None
    if due_time is not None and due_time.tzinfo is None:
        due_time = due_time.replace(tzinfo=now.tzinfo)
    if due_time is None or due_time <= now:
        entry["updated_at"] = now.isoformat(timespec="seconds")
        entry["eligible_at"] = dt.datetime.combine(now.date() + dt.timedelta(days=1),
                                                  dt.time(hour, minute), tzinfo=now.tzinfo).isoformat()


def _wait_entry(entry: dict, root: Path) -> None:
    """One waiting state; a free daily quota makes the item eligible immediately."""
    now = business_now().isoformat(timespec="seconds")
    entry.update(status="staged", defer_reason="execution_wait")
    entry.setdefault("staged_at", now)
    entry.setdefault("eligible_at", now)


def _fine_rescreen_due(entry: dict) -> bool:
    """Same-day waiting reuses AI; unknown or previous-day evidence is re-scored."""
    if entry.get("rescreen_pending"):
        return True
    stamp = entry.get("screened_at")
    try:
        then = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        now = business_now()
        then = then.astimezone(now.tzinfo) if then.tzinfo else then.replace(tzinfo=now.tzinfo)
        return then.date() != now.date()
    except (AttributeError, TypeError, ValueError):
        return True


STAGED_STATUS = "staged"          # All waiting candidates, including same-day work.
# 同一条候选一天内最多试几次。额度闸管总额，这里管单条别无限重试
# （2026-11-03 实测 deer-flow#7846 一天被 claim 7 次）。
MAX_ATTEMPTS_PER_CANDIDATE = 3
# 内部 fixture 仓库：自动 drain 永远跳过，演练走手动单条入口
STAGE_TTL_DAYS = 3                # 暂存超过这个天数就丢，避免拿陈旧的 issue 去修


def _stage_age_days(entry: dict) -> float:
    """候选进入暂存状态多久了（天）。时间戳缺失按 0 处理（当作刚暂存）。"""
    import datetime as dt
    ts = entry.get("staged_at") or entry.get("discovered_at")
    if not ts:
        return 0.0
    try:
        then = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if then.tzinfo is None:
            then = then.replace(tzinfo=business_now().tzinfo)
        return max(0.0, (business_now() - then).total_seconds() / 86400)
    except (ValueError, TypeError):
        return 0.0


def _eligible(entry: dict, *, refill_day: str = "") -> bool:
    import datetime as dt
    now = business_now()
    stamps = [entry.get("screen_retry_at"), entry.get("eligible_at")]
    if not stamps[1] and entry.get("status") == "staged":
        staged_at = entry.get("staged_at") or entry.get("discovered_at")
        if not staged_at:
            return False
        try:
            then = dt.datetime.fromisoformat(staged_at.replace("Z", "+00:00"))
            stamps[1] = dt.datetime.combine(then.date() + dt.timedelta(days=1), dt.time(4), tzinfo=now.tzinfo).isoformat()
        except (ValueError, TypeError):
            return False
    for index, ts in enumerate(stamps):
        if not ts:
            continue
        try:
            due = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if due.tzinfo is None:
                due = due.replace(tzinfo=now.tzinfo)
            refill = (index == 1 and entry.get("status") == "staged"
                      and entry.get("defer_reason") == "daily_quota"
                      and refill_day == now.date().isoformat())
            if due > now and not refill:
                return False
        except (ValueError, TypeError):
            return False
    return True


def _order_candidates(entries: list[tuple[str, dict]],
                      expired_out: list[str] | None = None, *, refill_day: str = "",
                      ) -> list[tuple[str, dict]]:
    """Eligible waiting candidates share one FIFO, regardless of why they waited.

    Quota-deferred items wait until their due time, except same-day closure
    refills. A retry backoff remains binding. Expired items are returned to the
    caller for an explicit audited transition.
    """
    staged = []
    for key, e in entries:
        if e.get("status") == STAGED_STATUS:
            if _stage_age_days(e) > STAGE_TTL_DAYS:
                if expired_out is not None:
                    expired_out.append(key)
                continue
            if not _eligible(e, refill_day=refill_day):
                continue
            staged.append((key, e))
    staged.sort(key=lambda kv: candidate_fifo_key(kv[0], kv[1]))
    return staged


def open_scheduled_batch(root: Path) -> list[str]:
    """Authorize the due historical snapshot once per business day, durably.

    The dispatcher may be busy at the scheduled time. Its snapshot can continue
    afterwards, but ordinary polling never enrolls additional historical items.
    """
    import yaml
    now = business_now()
    cfg = root / "configs/dashboard.yaml"
    settings = (yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}) if cfg.exists() else {}
    scheduled = now.replace(hour=int(settings.get("staged_flush_hour_local", 4)),
                            minute=int(settings.get("staged_flush_minute_local", 0)),
                            second=0, microsecond=0)
    if now < scheduled:
        return []
    day = now.date().isoformat()
    marker = root / "state/staged_consumption.json"
    with queue_transaction(root) as queue:
        previous = json.loads(marker.read_text(encoding="utf-8")) if marker.exists() else {}
        if previous.get("day") == day:
            # Recover a crash between writing the marker and committing the queue.
            for key in previous.get("candidate_ids", []):
                entry = queue.get(key) or {}
                if entry.get("status") == "staged" and not entry.get("dispatch_started_at"):
                    entry["scheduled_consume_day"] = day
            return []
        keys = []
        for key, entry in _order_candidates(list(queue.items())):
            if not _discovered_today(entry) and not entry.get("dispatch_started_at"):
                entry["scheduled_consume_day"] = day
                keys.append(key)
        atomic_json_write(marker, {"day": day, "candidate_ids": keys})
    log(root, "scheduled_consumption", day, f"authorized {len(keys)} historical candidates")
    return keys


def _discovered_today(entry: dict) -> bool:
    stamp = entry.get("discovered_at")
    try:
        then = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        now = business_now()
        then = then.astimezone(now.tzinfo) if then.tzinfo else then.replace(tzinfo=now.tzinfo)
        return then.date() == now.date()
    except (AttributeError, TypeError, ValueError):
        return False


def _dispatch_candidates(ordered: list[tuple[str, dict]], *, refill_day: str = "",
                         manual: bool = False) -> list[tuple[str, dict]]:
    if manual:
        return ordered
    day = business_now().date().isoformat()
    fresh, historical = [], []
    for item in ordered:
        entry = item[1]
        if _discovered_today(entry):
            fresh.append(item)
        elif refill_day == day or entry.get("scheduled_consume_day") == day:
            historical.append(item)
    return fresh + historical


def drain_ai_graded(root: Path, limit: int = 3, dry_run: bool = True,
                    max_new_claims: int = 0, *, rescreen: bool = False) -> list[dict]:
    """扫描候选池：AI 判定可处理、无客观否决、未处理过的 → 逐条走后端 pipeline。

    2026-10-30（用户裁定）：入口只看 AI 的二元判断。规则打分那套加分表
    （reactions/龄/标签权重）已删除——它是在 AI 评分之前写的启发式，等于对
    AI 的判断再加一道更粗糙的复核，两层经常互相打架。规则只留客观事实否决
    （blockers 非空 = 已有 PR / 认领信号 / 纯文档 / 计费抱怨这类不需要判断的事实）。

    普通派发优先选择今天发现的候选，历史项必须有当日定时批次授权或
    当天 PR 关闭释放额度。rescreen=True 是显式手动消费，按 FIFO 重筛。
    max_new_claims 保留旧调用参数；实际额度由每日发布账本原子判断。

    内部测试仓库（fixture）不参与自动 drain：P5 演练用手动触发，演练轮也
    烧 Actions 分钟（2026-10-31 审计发现 3 条 fixture 失败记录）。手动验证
    走 candidate_pipeline 单条入口，不走这条。
    """
    root = _data_root(root)
    qpath = root / "queue" / "candidates.json"
    q = json.loads(qpath.read_text(encoding="utf-8"))
    ACTIVE = (STAGED_STATUS,)
    eligible: list[tuple[str, dict]] = []
    for key, e in q.items():
        if e.get("status") not in ACTIVE:
            continue
        if e.get("dispatch_job_id") or e.get("dispatch_started_at"):
            continue
        if not monitored_repo(e.get("repo", "")):
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

    expired: list[str] = []
    refill_day = ""
    db = root / "state/scout_v2.sqlite3"
    if db.exists():
        ledger = ClaimLedger(db)
        try:
            eng, err = PolicyEngine.load(root)
            day = business_now(eng.tz_name).date().isoformat()
            if not err and ledger.has_publication_release(day):
                refill_day = day
        finally:
            ledger.close()
    ordered = _order_candidates(eligible, expired_out=expired, refill_day=refill_day)
    ordered = _dispatch_candidates(ordered, refill_day=refill_day, manual=rescreen)
    if expired and not dry_run:
        for key in expired:
            mark_candidate(root, key, "stage_expired")
        log(root, "stage_expired", f"{len(expired)} candidates",
            f"older than {STAGE_TTL_DAYS}d: " + ", ".join(expired[:5]))
    # A batch size is not a daily quota. Only a failed atomic claim defers work.
    picked = [e for _, e in ordered[:max(0, limit)]]

    if dry_run:
        return [{"id": e["id"], "repo": e["repo"], "number": e["number"],
                 "status": e.get("status"),
                 "ai": (e.get("ai_verdict") or {}).get("reason", "")[:60],
                 "dry_run": True} for e in picked]
    out = []
    for e in picked:
        key = str(e.get("id", ""))
        try:
            out.append(candidate_pipeline(root, e, rescreen=rescreen))
        except Exception as ex:  # noqa: BLE001
            log(root, "crash", key,
                f"unhandled: {type(ex).__name__}: {str(ex)[:200]}")
            _emergency_release(root, key)
            out.append({"id": key, "crash": f"{type(ex).__name__}: {str(ex)[:150]}"})
    return out
