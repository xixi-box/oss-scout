"""oss-scout v2 Web Dashboard（只读监控）。

启动：python -m dashboard.app  → http://127.0.0.1:8787
安全：全 Jinja2 自动转义；无 POST 写路由（统一 501）；日志查看有路径校验；token 打码。
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import data as D

PROJECT_ROOT = Path(__file__).resolve().parent.parent

BASE_DIR = Path(__file__).resolve().parent
templates_dir = BASE_DIR / "templates"
static_dir = BASE_DIR / "static"


def create_app(root: Path | None = None, gh_runner=None, modal_runner=None) -> FastAPI:
    root = Path(root) if root else Path(
        os.environ.get("OSSCOUT_DASHBOARD_ROOT", PROJECT_ROOT))
    app = FastAPI(title="oss-scout Dashboard", docs_url=None, redoc_url=None)
    ds = D.DataSource(root, gh_runner=gh_runner, modal_runner=modal_runner)
    app.state.ds = ds
    app.state.root = root
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    tpl = Jinja2Templates(directory=str(templates_dir))
    tpl.env.filters["mask"] = D.mask
    app.state.templates = tpl

    def render(request: Request, name: str, ctx: dict, status_code: int = 200):
        ctx.setdefault("request", request)
        return tpl.TemplateResponse(request, name, ctx, status_code=status_code)

    # ---------------- 1. 总览 ----------------
    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request):
        o = ds.overview()
        return render(request, "overview.html",
                      {"o": o, "recent": o["recent_worklog"], "active": "/"})

    @app.get("/fragment/overview-cards", response_class=HTMLResponse)
    def overview_cards(request: Request):
        """HTMX 每 20s 轮询的关键卡片片段。"""
        return render(request, "_overview_cards.html", {"o": ds.overview()})

    @app.get("/fragment/overview-worklog", response_class=HTMLResponse)
    def overview_worklog(request: Request):
        o = ds.overview()
        return render(request, "_worklog_recent.html",
                      {"recent": o["recent_worklog"]})

    # ---------------- 2. 候选池 ----------------
    @app.get("/candidates", response_class=HTMLResponse)
    def candidates(request: Request, q: str = "", lane: str = "", class_: str = Query("", alias="class"),
                   status: str = "", repo: str = ""):
        items = ds.filter_candidates(ds.candidates(), q=q, lane=lane, cls=class_,
                                     status=status, repo=repo)
        return render(request, "candidates.html", {
            "items": items, "total": len(ds.candidates()),
            "q": q, "lane": lane, "class_": class_, "status": status, "repo": repo,
            "statuses": sorted({str(c.get("status", "")) for c in ds.candidates().values()}),
            "repos": sorted({c.get("repo", "") for c in ds.candidates().values()}),
            "bug_jobs": D.bug_lane_jobs(ds),
            "quote_key": ds.quote_key, "active": "/candidates",
        })

    @app.get("/candidates/{key:path}/detail", response_class=HTMLResponse)
    def candidate_detail(request: Request, key: str):
        c = ds.candidate(key)
        if c is None:
            return HTMLResponse("<td colspan='9' class='muted'>候选不存在</td>", status_code=404)
        return render(request, "_candidate_detail.html", {"c": c, "key": key})

    # ---------------- 3. 任务中心 ----------------
    @app.get("/jobs", response_class=HTMLResponse)
    def jobs(request: Request):
        return render(request, "jobs.html", {
            "jobs": ds.jobs(), "active": "/jobs",
        })

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_detail(request: Request, job_id: str):
        j = ds.job(job_id)
        if j is None:
            return render(request, "job_detail.html", {"job": None, "artifact": None,
                                                       "active": "/jobs"}, status_code=404)
        artifact = None
        payload = j.get("payload") or {}
        art_dir = str(payload.get("artifact_dir") or payload.get("artifacts_dir") or "")
        if art_dir and (ds.root / "artifacts" / art_dir).is_dir():
            art = next((a for a in ds.artifacts() if a["name"] == art_dir), None)
            artifact = art
        return render(request, "job_detail.html", {"job": j, "artifact": artifact,
                                                   "active": "/jobs"})

    # ---------------- 4. PR 管理 ----------------
    @app.get("/prs", response_class=HTMLResponse)
    def prs(request: Request):
        import datetime as dt
        repos = ds.own_repos()
        repo_prs: list[dict] = []
        any_unknown = not repos
        now = dt.datetime.now(dt.timezone.utc)
        for repo in repos:
            pr_list = ds.pr_list(repo)
            if pr_list is None:
                any_unknown = True
                repo_prs.append({"repo": repo, "prs": None})
                continue
            for pr in pr_list:
                created_raw = pr.get("createdAt")
                deadline_left = None
                if created_raw:
                    try:
                        created = dt.datetime.fromisoformat(
                            str(created_raw).replace("Z", "+00:00"))
                        deadline = created + dt.timedelta(hours=48)
                        delta = deadline - now
                        deadline_left = delta.total_seconds() / 3600.0
                    except ValueError:
                        deadline_left = None
                comments = pr.get("comments") or []
                rollup = pr.get("statusCheckRollup") or []
                conclusions = []
                for item in rollup:
                    if isinstance(item, dict):
                        conclusions.append((item.get("conclusion")
                                            or item.get("status") or "").upper())
                if not rollup:
                    ci = "—"
                elif all(c in ("SUCCESS", "NEUTRAL", "SKIPPED") for c in conclusions):
                    ci = "通过"
                else:
                    ci = "失败/进行中"
                pr["_repo"] = repo
                pr["_deadline_left_h"] = deadline_left
                pr["_has_review"] = bool(comments)
                pr["_ci"] = ci
            repo_prs.append({"repo": repo, "prs": pr_list})
        return render(request, "prs.html", {
            "repo_prs": repo_prs, "any_unknown": any_unknown,
            "merge_history": ds.merge_history(), "active": "/prs",
        })

    # ---------------- 5. 策略页 ----------------
    @app.get("/policies", response_class=HTMLResponse)
    def policies(request: Request):
        return render(request, "policies.html",
                      build_policies_ctx(ds) | {"active": "/policies"})

    # ---------------- 6. 额度页 ----------------
    @app.get("/quota", response_class=HTMLResponse)
    def quota(request: Request):
        modal = ds.modal_usage()
        actions = None
        repos = ds.own_repos()
        if repos:
            actions = ds.actions_runs(repos[0])
        return render(request, "quota.html", {
            "modal": modal,
            "measured_cost": ds.measured_cost_total(),
            "actions": actions,
            "actions_repo": repos[0] if repos else None,
            "opencode": ds.opencode_stats(),
            "active": "/quota",
        })

    # ---------------- 7. 日志页 ----------------
    @app.get("/logs", response_class=HTMLResponse)
    def logs(request: Request, page: int = 1, action: str = "", error: str = ""):
        records = list(reversed(ds.worklog()))
        if action:
            records = [r for r in records if action.lower() in str(r.get("action", "")).lower()]
        if error == "1":
            def _is_err(r):
                blob = (str(r.get("result", "")) + " " + str(r.get("detail", ""))).lower()
                return any(k in blob for k in ("fail", "error", "403", "blocker"))
            records = [r for r in records if _is_err(r)]
        per_page = 50
        total = len(records)
        pages = max(1, (total + per_page - 1) // per_page)
        page = max(1, min(page, pages))
        chunk = records[(page - 1) * per_page: page * per_page]
        return render(request, "logs.html", {
            "records": chunk, "page": page, "pages": pages, "total": total,
            "action": action, "error": error,
            "runs_files": ds.runs_files(), "active": "/logs",
        })

    @app.get("/logs/view", response_class=HTMLResponse)
    def logs_view(request: Request, name: str = ""):
        try:
            text = ds.runs_read(name)
        except (ValueError, OSError):
            return HTMLResponse("<p class='muted'>非法文件名或文件不存在</p>", status_code=400)
        return render(request, "log_view.html", {"name": name, "text": D.mask(text),
                                                 "active": "/logs"})

    # ---------------- artifact 文件查看 ----------------
    @app.get("/artifacts/{name}/{filename}", response_class=HTMLResponse)
    def artifact_view(request: Request, name: str, filename: str):
        hit = ds.artifact_file(name, filename)
        if hit is None:
            return HTMLResponse("<p class='muted'>文件不存在或路径非法</p>", status_code=404)
        return render(request, "log_view.html", {"name": f"artifacts/{name}/{hit[0]}",
                                                 "text": D.mask(hit[1]), "active": "/logs"})

    # ---------------- 无 POST 写路由 ----------------
    @app.get("/{rest:path}")
    def get_fallback(rest: str):
        return JSONResponse({"detail": "not found"}, status_code=404)

    @app.post("/{rest:path}")
    @app.put("/{rest:path}")
    @app.delete("/{rest:path}")
    def no_writes(rest: str):
        return JSONResponse({"detail": "dashboard is read-only"}, status_code=501)

    return app


# ---------------- 策略页构建 ----------------

LANE_RULES = {
    "open": {
        "allow": ["直接认领（每业务日至多 1 个）", "最小修复 + 回归测试后直接开 PR"],
        "deny": ["永不 force-push / 合并上游 / 跳过 CI", "龄 <2h 的新鲜 issue 战略放弃"],
    },
    "gated": {
        "allow": ["发分析评论对齐方案（无求指派字眼）", "等 maintainer 指派后再实现"],
        "deny": ["禁止直接开 PR", "禁止 'please assign me' 类评论", "禁止 force-push / 合并上游"],
    },
    "restricted": {
        "allow": ["只消费 maintainer_confirm 或 help wanted 的候选"],
        "deny": ["其余候选一律不认领", "PR 必须带测试（CONTRIBUTING checklist）"],
    },
    "observe": {
        "allow": ["仅发现与监控"],
        "deny": ["任何 GitHub 写动作"],
    },
}


def build_policies_ctx(ds: D.DataSource) -> dict:
    repo_yaml = ds.repositories_yaml().get("repositories") or {}
    cfg = ds.config_json()
    health, health_expired = ds.health()
    health_repos = (health or {}).get("repos") or {}

    pool_of: dict[str, str] = {}
    extra: dict[str, dict] = {}
    for pool_key, lane in (("open_pool", "open"), ("gated_pool", "gated"),
                           ("restricted_pool", "restricted"), ("observe_only", "observe")):
        for item in cfg.get(pool_key) or []:
            if isinstance(item, str):
                pool_of[item] = lane
            else:
                pool_of[item.get("repo", "")] = lane
                extra[item.get("repo", "")] = item

    rows = []
    all_repos = sorted(set(repo_yaml) | set(pool_of))
    for repo in all_repos:
        y = repo_yaml.get(repo) or {}
        lane = y.get("lane", "")
        pool_lane = pool_of.get(repo)
        conflict = bool(pool_lane and lane and pool_lane != lane)
        missing = (not lane) or (not pool_lane)
        rules = LANE_RULES.get(lane or pool_lane or "", {
            "allow": [], "deny": ["lane 未知 → hold_for_review（fail-closed）"]})
        h = health_repos.get(repo)
        if h is None:
            health_note, health_warn = "health 无记录", True
        else:
            flags = ", ".join((h.get("policy") or {}).get("flags") or []) or "none"
            advisory = h.get("lane_advisory", "?")
            health_note = f"flags: {flags} · advisory: {advisory}"
            health_warn = bool(advisory != lane)
        if health_expired:
            health_note += f" · health 过期（{ds.health_date()}），待重跑"
        rows.append({
            "repo": repo, "lane": lane or pool_lane or "?",
            "yaml_lane": lane, "pool_lane": pool_lane,
            "pr_rules": y.get("pr_rules") or extra.get(repo, {}).get("pr_rules") or [],
            "notes": extra.get(repo, {}).get("notes", ""),
            "gate": extra.get(repo, {}).get("gate", ""),
            "only": y.get("only") or extra.get(repo, {}).get("only", ""),
            "allow": rules["allow"], "deny": rules["deny"],
            "conflict": conflict, "missing": missing,
            "health_note": health_note, "health_warn": health_warn,
            "internal": bool(y.get("internal_test_repo")),
        })
    return {"rows": rows, "health_expired": health_expired, "health_date": ds.health_date()}


app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8787)
