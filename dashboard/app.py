"""oss-scout v2 Web Dashboard（登录 + 只读监控 + 受审计的配置写入）。

启动：python -m dashboard.app  → http://127.0.0.1:8787
安全：登录会话（HMAC 签名 cookie）；全 Jinja2 自动转义；写端点全部过认证、
校验并逐条写 worklog 审计；日志查看有路径校验；token 打码。
密码来源：环境变量 DASHBOARD_PASSWORD 优先，否则自动生成
state/dashboard_password.txt（gitignore，首启时展示一次）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

import yaml
from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import data as D

PROJECT_ROOT = Path(__file__).resolve().parent.parent

BASE_DIR = Path(__file__).resolve().parent
templates_dir = BASE_DIR / "templates"
static_dir = BASE_DIR / "static"

SESSION_COOKIE = "os_session"
SESSION_TTL = 7 * 24 * 3600

VALID_LANES = ("open", "gated", "restricted", "observe")
VALID_MODES = ("scan", "reproduce", "repair", "review", "ci")
VALID_BACKENDS = ("modal", "actions")
VALID_FEATURES = ("bug_hunter_light", "bug_hunter_deep_modal",
                  "actions_backend", "opencode_models")


def _secret(root: Path) -> bytes:
    """会话签名密钥：每实例生成一次，存 state/dashboard_session_key。"""
    p = root / "state" / "dashboard_session_key"
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.write_text(secrets.token_hex(32), encoding="utf-8")
    return p.read_text(encoding="utf-8").encode()


def _dashboard_password(root: Path) -> str:
    env = os.environ.get("DASHBOARD_PASSWORD")
    if env:
        return env
    p = root / "state" / "dashboard_password.txt"
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        pw = secrets.token_urlsafe(12)
        p.write_text(pw + "\n", encoding="utf-8")
        print(f"[dashboard] 首次生成登录密码 → {p}\n[dashboard] password: {pw}")
    return p.read_text(encoding="utf-8").strip()


def _sign(secret: bytes, payload: str) -> str:
    return hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()[:32]


def _make_session(secret: bytes) -> str:
    payload = json.dumps({"ts": int(time.time())})
    raw = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"{raw}.{_sign(secret, raw)}"


def _check_session(secret: bytes, token: str) -> bool:
    try:
        raw, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(sig, _sign(secret, raw)):
            return False
        pad = "=" * (-len(raw) % 4)
        payload = json.loads(base64.urlsafe_b64decode(raw + pad))
        return time.time() - payload["ts"] < SESSION_TTL
    except Exception:  # noqa: BLE001
        return False


def _audit(root: Path, action: str, target: str, result: str) -> None:
    """所有 Web 写操作的唯一审计出口（与 CLI/agent 共用 worklog 账本）。"""
    import datetime as dt
    rec = {"ts": dt.datetime.now().isoformat(timespec="seconds"),
           "action": f"dashboard_{action}", "target": target, "result": result,
           "source": "web-ui"}
    with (root / "state" / "worklog.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    yaml.safe_load(tmp.read_text(encoding="utf-8"))  # 校验
    os.replace(tmp, path)


def _set_lane(root: Path, repo: str, lane: str) -> None:
    """写 configs/lane_overrides.yaml（叠加层，repo_policy.PolicyEngine 读取时合并）。"""
    p = root / "configs" / "lane_overrides.yaml"
    data = {}
    if p.exists():
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    data.setdefault("repositories", {})
    data["repositories"].setdefault(repo, {})
    data["repositories"][repo]["lane"] = lane
    data.setdefault("_meta", {})["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    _atomic_write_text(p, yaml.safe_dump(data, allow_unicode=True, sort_keys=True))


def _replace_toplevel_block(text: str, key: str, new_block: str) -> str:
    """替换 YAML 顶层键的整块内容（到下一个顶层键或 EOF），其余文本原样保留。"""
    import re
    pattern = re.compile(rf"(?ms)^{re.escape(key)}:[ \t]*\n(?:[ \t]+.*\n?)*")
    if not pattern.search(text):
        # 块不存在 → 追加到末尾
        return text.rstrip("\n") + "\n\n" + new_block
    return pattern.sub(new_block.replace("\\", "\\\\"), text, count=1)


def _set_routing(root: Path, mode: str, primary: str, fallback: str) -> None:
    """更新 configs/v2.yaml 的 routing 块：解析→改值→整块重渲染（流式），注释会丢
    routing 块内的行注（可接受：语义由本函数与测试保证），其余注释原样保留。"""
    p = root / "configs" / "v2.yaml"
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    routing = data.get("routing") or {}
    routing[mode] = {"primary": primary, "fallback": fallback}
    lines = ["routing:"]
    for m in ("scan", "reproduce", "repair", "review", "ci"):
        e = routing.get(m) or {"primary": "modal", "fallback": "actions"}
        lines.append(f"  {m}:      {{primary: {e['primary']},   fallback: {e['fallback']}}}")
    _atomic_write_text(p, _replace_toplevel_block(
        p.read_text(encoding="utf-8"), "routing", "\n".join(lines) + "\n"))


def _set_feature(root: Path, key: str, value: bool) -> None:
    p = root / "configs" / "v2.yaml"
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    features = data.get("features") or {}
    features[key] = value
    lines = ["features:"]
    for k in VALID_FEATURES:
        lines.append(f"  {k}: {str(features.get(k, False)).lower()}")
    _atomic_write_text(p, _replace_toplevel_block(
        p.read_text(encoding="utf-8"), "features", "\n".join(lines) + "\n"))


def create_app(root: Path | None = None, gh_runner=None, modal_runner=None,
               auth_enabled: bool = True, password: str | None = None) -> FastAPI:
    root = Path(root) if root else Path(
        os.environ.get("OSSCOUT_DASHBOARD_ROOT", PROJECT_ROOT))
    app = FastAPI(title="oss-scout Dashboard", docs_url=None, redoc_url=None)


    def _load_candidate(root_path: Path, key: str) -> dict | None:
        """从队列重读一条候选（线程启动时用最新状态，不用轮询时的快照）。"""
        try:
            q = json.loads((root_path / "queue" / "candidates.json")
                           .read_text(encoding="utf-8"))
            ent = q.get(key)
            if isinstance(ent, dict):
                ent.setdefault("id", key)
                return ent
        except Exception:  # noqa: BLE001
            pass
        return None

    # ---- 修复派发器（ingest 与轮询共用）----
    # pipeline 是长任务（几十分钟），不能让 HTTP 请求或轮询等它：
    # 每条候选一个 daemon 线程，inflight 表 + 锁防重复派发。
    import threading as _thr
    _inflight: dict[str, object] = {}
    _inflight_lock = _thr.Lock()
    _MAX_CONCURRENT = 3   # 同时最多 3 条，避免 VPS（2 核 1.6G）被打满

    def _run_one(entry: dict) -> None:
        key = str(entry.get("id", ""))
        try:
            from scout_v2.workers.auto_pipeline import candidate_pipeline
            out = candidate_pipeline(root, entry)
            _audit(root, "auto_repair", key, json.dumps(out, ensure_ascii=False)[:400])
        except Exception as e:  # noqa: BLE001
            try:
                _audit(root, "auto_repair_error", key, str(e)[:200])
            except Exception:  # noqa: BLE001
                pass
        finally:
            with _inflight_lock:
                _inflight.pop(key, None)

    def _dispatch(keys: list[str] | None = None, limit: int = 8) -> list[str]:
        """派发候选去修复。keys 为空时从队列按 AI 判定挑。

        返回本次新派发的 key 列表。不阻塞：线程启动即返回。
        """
        started: list[str] = []
        if os.environ.get("OSSCOUT_DISABLE_AUTO_REPAIR") == "1":
            return started
        try:
            with _inflight_lock:
                room = _MAX_CONCURRENT - len(_inflight)
            if room <= 0:
                return started
            if keys is None:
                from scout_v2.workers.auto_pipeline import drain_ai_graded
                cands = [c["id"] for c in
                         drain_ai_graded(root, limit=room + len(_inflight),
                                         dry_run=True)]
            else:
                cands = list(keys)
            for key in cands:
                with _inflight_lock:
                    if len(_inflight) >= _MAX_CONCURRENT:
                        break
                    if key in _inflight:
                        continue
                    ent = _load_candidate(root, key)
                    if ent is None:
                        continue
                    th = _thr.Thread(target=_run_one, args=(ent,), daemon=True,
                                     name=f"repair-{key}")
                    _inflight[key] = th
                th.start()
                started.append(key)
        except Exception as e:  # noqa: BLE001 — 派发失败不影响主流程
            try:
                _audit(root, "dispatch_error", "-", str(e)[:200])
            except Exception:  # noqa: BLE001
                pass
        return started

    def _auto_repair_loop():
        """兜底轮询：定时扫候选池，AI 判定可用的就派发去修复。

        正常情况下候选在 ingest 时就已经立即派发了（见 api_ingest_issues），
        这个轮询只处理"上次因并发已满没派出去"的漏网之鱼，以及人工在界面上
        改状态后需要重新拾起的情况。
        间隔可配（configs/dashboard.yaml: auto_repair_seconds，0=关闭）。
        """
        import time as _t

        def _loop():
            if os.environ.get("OSSCOUT_DISABLE_AUTO_REPAIR") == "1":
                return   # 运维/验收模式：手动触发 pipeline
            _t.sleep(30)   # 启动后先等容器就绪
            while True:
                try:
                    cfg = D.dashboard_settings(ds)
                    interval = int(cfg.get("auto_repair_seconds", 60))
                    if interval <= 0:
                        _t.sleep(60)
                        continue
                    _dispatch(keys=None)
                    _t.sleep(interval)
                except Exception as e:  # noqa: BLE001 — 永不因单次异常停摆
                    try:
                        _audit(root, "auto_repair_error", "-", str(e)[:200])
                    except Exception:  # noqa: BLE001
                        pass
                    _t.sleep(120)

        _thr.Thread(target=_loop, daemon=True).start()

    def _cf_poll_loop():
        """按配置间隔（默认 20s）从 CF KV 拉取 Worker 发现（失败静默，下轮重试）。"""
        import threading as _th
        import time as _t
        def _loop():
            while True:
                try:
                    out = D.cloudflare_pull(ds)
                    if "error" not in out:
                        (root / "state" / "cloudflare_last_pull.json").write_text(
                            json.dumps({"at": _t.strftime("%Y-%m-%dT%H:%M:%S"),
                                        "out": out}, ensure_ascii=False),
                            encoding="utf-8")
                except Exception:  # noqa: BLE001
                    pass
                # 间隔可前端配置（configs/dashboard.yaml）；默认 20s
                # KV 读额度 10 万/天，20s 轮询 = 4320 次/天，仅占 4%
                _t.sleep(D.cf_pull_seconds(ds))
        _th.Thread(target=_loop, daemon=True).start()

    ds = D.DataSource(root, gh_runner=gh_runner, modal_runner=modal_runner)

    def _reap_orphan_claim():
        """启动时清理孤儿认领：容器若在上次 pipeline 执行中重启（如部署覆盖），
        sqlite 里已提交的认领行没有对应的终态记录，会把当日额度占死
        （2026-10-27 opencode#59317 事故）。判定：认领的候选 status 仍是
        new/recheck_failed（无终态标记）→ 释放，自动线程 5 分钟内会重试。"""
        try:
            import datetime as _dt
            import sqlite3
            db = root / "state" / "scout_v2.sqlite3"
            if not db.exists():
                return
            conn = sqlite3.connect(str(db), timeout=15)
            try:
                today = _dt.date.today().isoformat()
                rows = conn.execute(
                    "SELECT candidate_id FROM claim_days WHERE day=?", (today,)).fetchall()
                if not rows:
                    return
                q = json.loads((root / "queue" / "candidates.json").read_text(encoding="utf-8"))
                for (cid,) in rows:
                    entry = q.get(cid)
                    status = (entry or {}).get("status", "")
                    if status in ("new", "queued", "queued_hist", "recheck_failed"):
                        conn.execute("DELETE FROM claim_days WHERE day=? AND candidate_id=?",
                                     (today, cid))
                        _audit(root, "orphan_claim_reaped", cid,
                               f"container restarted mid-pipeline (status={status}); released")
                conn.commit()
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001 — 清理失败不阻塞启动
            try:
                _audit(root, "orphan_claim_reap_error", "-", str(e)[:150])
            except Exception:  # noqa: BLE001
                pass

    _reap_orphan_claim()
    _cf_poll_loop()   # 必须在 ds 创建后启动（依赖 ds 读配置）
    _auto_repair_loop()
    app.state.ds = ds
    app.state.root = root
    secret = _secret(root)
    passwd = password or _dashboard_password(root)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    tpl = Jinja2Templates(directory=str(templates_dir))
    tpl.env.filters["mask"] = D.mask
    app.state.templates = tpl

    def render(request: Request, name: str, ctx: dict, status_code: int = 200):
        ctx.setdefault("request", request)
        return tpl.TemplateResponse(request, name, ctx, status_code=status_code)

    # ---------------- 认证 ----------------
    PUBLIC_PATHS = ("/login", "/static", "/favicon.ico", "/api/ingest/")

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        path = request.url.path
        if not auth_enabled or any(path.startswith(p) for p in PUBLIC_PATHS):
            return await call_next(request)
        token = request.cookies.get(SESSION_COOKIE, "")
        if _check_session(secret, token):
            return await call_next(request)
        if path.startswith("/api/"):
            return JSONResponse({"detail": "unauthenticated"}, status_code=401)
        url = str(request.url_for("login_page")) + f"?next={path}"
        return RedirectResponse(url, status_code=303)

    @app.get("/login", response_class=HTMLResponse, name="login_page")
    def login_page(request: Request, next: str = "/"):
        return render(request, "login.html", {"next": next, "error": ""})

    @app.post("/login")
    def login(request: Request, next: str = Form("/"), password: str = Form("")):
        if not hmac.compare_digest(password, passwd):
            time.sleep(1.0)  # 简单防爆破
            return render(request, "login.html",
                          {"next": next, "error": "密码错误"}, status_code=401)
        resp = RedirectResponse(next or "/", status_code=303)
        resp.set_cookie(SESSION_COOKIE, _make_session(secret),
                        max_age=SESSION_TTL, httponly=True, samesite="lax")
        _audit(root, "login", "web-ui", "login ok")
        return resp

    @app.post("/logout")
    def logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(SESSION_COOKIE)
        return resp

    # ---------------- 受审计的写端点（登录后可用） ----------------
    @app.post("/api/policies/lane")
    def api_set_lane(request: Request, repo: str = Form(...), lane: str = Form(...)):
        if lane not in VALID_LANES:
            return JSONResponse({"detail": f"lane must be one of {VALID_LANES}"},
                                status_code=400)
        try:
            _set_lane(root, repo, lane)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"detail": str(e)[:200]}, status_code=500)
        result = f"lane={lane} written to lane_overrides (PolicyEngine merges on next load)"
        _audit(root, "set_lane", repo, result)
        return RedirectResponse("/policies", status_code=303)

    @app.post("/api/routing")
    def api_set_routing(request: Request, mode: str = Form(...),
                        primary: str = Form(...), fallback: str = Form(...)):
        if mode not in VALID_MODES:
            return JSONResponse({"detail": f"mode must be one of {VALID_MODES}"},
                                status_code=400)
        if primary not in VALID_BACKENDS or fallback not in VALID_BACKENDS:
            return JSONResponse({"detail": f"backends must be {VALID_BACKENDS}"},
                                status_code=400)
        try:
            _set_routing(root, mode, primary, fallback)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"detail": str(e)[:200]}, status_code=500)
        result = f"{mode}: primary={primary} fallback={fallback}"
        _audit(root, "set_routing", mode, result)
        return RedirectResponse("/quota", status_code=303)

    @app.post("/api/features")
    def api_set_feature(request: Request, key: str = Form(...), value: str = Form(...)):
        if key not in VALID_FEATURES:
            return JSONResponse({"detail": f"key must be one of {VALID_FEATURES}"},
                                status_code=400)
        val = value.lower() in ("true", "1", "on")
        try:
            _set_feature(root, key, val)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"detail": str(e)[:200]}, status_code=500)
        result = f"{key}={val}"
        _audit(root, "set_feature", key, result)
        return RedirectResponse("/quota", status_code=303)

    @app.post("/api/opencode/check")
    def api_opencode_check():
        """点击"检测登录态"：跑一次最小真实模型调用（约 $0.002 订阅额度）。"""
        from scout_v2.workers.opencode_client import OpenCodeClient
        from scout_v2.workers.repair import resolve_model
        try:
            model = resolve_model()
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "detail": f"model resolve: {e}"})
        try:
            client = OpenCodeClient(timeout_seconds=120)
            ok, detail = client.run(model, "Reply with the single word OK and nothing else.")
        except Exception as e:  # noqa: BLE001
            ok, detail = False, str(e)[:200]
        _audit(root, "opencode_check", model, ("ok" if ok else "fail: " + detail[:120]))
        (root / "state" / "dashboard_opencode_state.json").write_text(
            json.dumps({"ok": ok, "model": model,
                        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S")},
                       ensure_ascii=False), encoding="utf-8")
        return JSONResponse({"ok": ok, "model": model, "detail": detail[:200]})

    @app.post("/api/opencode/logout")
    def api_opencode_logout():
        """登录态正常时显示：退出 OpenCode CLI 登录（opencode providers logout）。"""
        import subprocess as sp
        p = sp.run(["opencode", "providers", "logout"], capture_output=True,
                   text=True, encoding="utf-8", errors="replace", timeout=60)
        ok = p.returncode == 0
        detail = (p.stdout or p.stderr or "").strip()[:200]
        _audit(root, "opencode_logout", "opencode-go", ("ok" if ok else "fail: " + detail))
        (root / "state" / "dashboard_opencode_state.json").write_text(
            json.dumps({"ok": False, "model": "",
                        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S")},
                       ensure_ascii=False), encoding="utf-8")
        return JSONResponse({"ok": ok, "detail": detail})

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
                elif any(c in ("FAILURE", "CANCELLED", "TIMED_OUT") for c in conclusions):
                    ci = "失败"
                else:
                    ci = "进行中"
                pr["_repo"] = repo
                pr["_deadline_left_h"] = deadline_left
                pr["_has_review"] = bool(pr.get("reviews") or comments)
                pr["_ci"] = ci
            repo_prs.append({"repo": repo, "prs": pr_list})
        external = ds.my_external_prs(limit=50)
        if external is None:
            any_unknown = True
        my_login = ds.me_login()
        return render(request, "prs.html", {
            "repo_prs": repo_prs, "any_unknown": any_unknown,
            "external_prs": external, "my_login": my_login,
            "merge_history": ds.merge_history(), "active": "/prs",
            "settings": D.dashboard_settings(ds),
        })

    @app.get("/prs/{repo_slug}/{number}/detail", response_class=HTMLResponse)
    def pr_detail_fragment(request: Request, repo_slug: str, number: int):
        """PR 详情片段：完整描述、全部评论、review 意见、改动文件。"""
        repo = repo_slug.replace("__", "/")
        detail = ds.pr_detail(repo, number)
        if detail is None:
            return HTMLResponse("<p class='muted'>PR 详情获取失败（gh 查询失败或 PR 不存在）</p>",
                                status_code=404)
        # 48h 响应期限
        deadline_h = None
        created = detail.get("createdAt")
        if created:
            try:
                import datetime as _dt
                c = _dt.datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                deadline_h = (c + _dt.timedelta(hours=48)
                              - _dt.datetime.now(_dt.timezone.utc)).total_seconds() / 3600
            except ValueError:
                pass
        return render(request, "_pr_detail.html", {
            "pr": detail, "repo": repo, "deadline_h": deadline_h})

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
        v2 = {}
        vp = root / "configs" / "v2.yaml"
        if vp.exists():
            v2 = yaml.safe_load(vp.read_text(encoding="utf-8")) or {}
        return render(request, "quota.html", {
            "modal": modal,
            "measured_cost": ds.measured_cost_total(),
            "actions": actions,
            "actions_repo": repos[0] if repos else None,
            "opencode": ds.opencode_stats(),
            "routing": (v2.get("routing") or {}),
            "features": (v2.get("features") or {}),
            "oc_logged_in": _go_login_state(),
            "go_models": _go_models_cached(root),
            "scorer_cfg": (yaml.safe_load((root / "configs" / "scorer.yaml").read_text(
                encoding="utf-8")) or {}) if (root / "configs" / "scorer.yaml").exists()
                else {"model": "deepseek-v4.1-flash"},
            "cf": D.cloudflare_cfg(ds),
            "last_pull": D.cloudflare_last_pull(ds),
            "settings": D.dashboard_settings(ds),
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

    @app.get("/api/opencode/models")
    def api_opencode_models():
        from scout_v2.workers.issue_scorer import GoClient, load_scorer_config
        cfg = load_scorer_config(root)
        if not cfg.get("api_key"):
            return JSONResponse({"models": [], "logged_in": False})
        c = GoClient(key=cfg["api_key"], model=cfg.get("model", "deepseek-v4.1-flash"))
        models = c.list_models()
        return JSONResponse({"models": models,
                             "logged_in": bool(models),
                             "current": cfg.get("model", "deepseek-v4.1-flash")})

    @app.post("/api/opencode/model")
    def api_opencode_model(request: Request, model: str = Form(...)):
        from scout_v2.workers.issue_scorer import DEFAULT_MODEL
        if not model or "/" in model or model not in _go_models_cached(root):
            return JSONResponse({"detail": "unknown model"}, status_code=400)
        p = root / "configs" / "scorer.yaml"
        cfg = {}
        if p.exists():
            cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        cfg["model"] = model or DEFAULT_MODEL
        _atomic_write_text(p, yaml.safe_dump(cfg, allow_unicode=True, sort_keys=True))
        _audit(root, "set_scorer_model", model, "scorer.yaml updated")
        return RedirectResponse("/quota", status_code=303)

    def _go_login_state() -> bool:
        from scout_v2.workers.issue_scorer import GoClient, load_scorer_config
        cfg = load_scorer_config(root)
        if not cfg.get("api_key"):
            return False
        def _probe():
            return GoClient(key=cfg["api_key"], model=cfg.get("model", "deepseek-v4.1-flash")).list_models()
        return bool(D.DataSource._cached(ds, "go_login", 300.0, _probe))

    def _go_models_cached(rt: Path) -> list:
        cacher = app.state.ds
        from scout_v2.workers.issue_scorer import GoClient, load_scorer_config
        cfg = load_scorer_config(rt)
        if not cfg.get("api_key"):
            return []
        def _fetch():
            return GoClient(key=cfg["api_key"],
                            model=cfg.get("model", "deepseek-v4.1-flash")).list_models()
        return D.DataSource._cached(cacher, "go_models", 600.0, _fetch) or []

    @app.post("/api/cloudflare/config")
    def api_cloudflare_config(request: Request, api_token: str = Form(""),
                              account_id: str = Form(""),
                              kv_namespace_id: str = Form(""),
                              repos: str = Form("")):
        cfg = {"api_token": api_token.strip(),
               "account_id": account_id.strip(),
               "kv_namespace_id": kv_namespace_id.strip(),
               "repos": [r.strip() for r in repos.replace(",", "\n").split() if r.strip()],
               "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        _atomic_write_text(root / "configs" / "cloudflare.yaml",
                           yaml.safe_dump(cfg, allow_unicode=True, sort_keys=True))
        _audit(root, "cloudflare_config", "configs/cloudflare.yaml",
               f"repos={len(cfg['repos'])} token_set={bool(cfg['api_token'])}")
        return RedirectResponse("/quota", status_code=303)

    @app.get("/api/pipeline/preview")
    def api_pipeline_preview():
        from scout_v2.workers.auto_pipeline import drain_ai_graded
        return JSONResponse({"would_process": drain_ai_graded(root, limit=3, dry_run=True)})

    @app.post("/api/pipeline/run")
    def api_pipeline_run(request: Request):
        from scout_v2.workers.auto_pipeline import drain_ai_graded
        out = drain_ai_graded(root, limit=1, dry_run=False)
        _audit(root, "pipeline_run", "auto", json.dumps(out, ensure_ascii=False)[:400])
        return JSONResponse({"results": out})

    # ---------------- Cloudflare Worker 直推入口（KV 免费额度不够时的主通道） ----------------
    @app.post("/api/ingest/issues")
    async def api_ingest_issues(request: Request):
        """Worker 直接回传 issue（免 KV 写额度）。

        安全：X-Ingest-Token 对比 configs/cloudflare.yaml:ingest_token；
        请求体是 Worker 自己的 JSON（不可信），只取白名单字段，评分走本地规则 + AI。
        """
        cfg = D.cloudflare_cfg(ds)
        expect = (cfg.get("ingest_token") or "").strip()
        got = request.headers.get("X-Ingest-Token", "").strip()
        if not expect:
            return JSONResponse({"detail": "ingest_token not configured"}, status_code=503)
        if not hmac.compare_digest(got, expect):
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        try:
            body = json.loads((await request.body()) or b"{}")
        except Exception:  # noqa: BLE001
            return JSONResponse({"detail": "bad json"}, status_code=400)
        items = body.get("issues")
        if not isinstance(items, list):
            return JSONResponse({"detail": "issues must be a list"}, status_code=400)
        # 字段收敛：只保留白名单，防注入超大/异常结构
        clean = []
        for it in items[:200]:
            if not isinstance(it, dict):
                continue
            clean.append({
                "repo": str(it.get("repo", ""))[:120],
                "number": int(it.get("number") or 0),
                "title": str(it.get("title", ""))[:400],
                "url": str(it.get("url", ""))[:400],
                "created_at": str(it.get("created_at", ""))[:40],
                "labels": [str(x)[:60] for x in (it.get("labels") or [])][:20],
                "reactions": int(it.get("reactions") or 0),
                "comments": int(it.get("comments") or 0),
                "body_excerpt": str(it.get("body_excerpt", ""))[:8000],
            })
        queued, added = D.ingest_cloudflare_issues(ds, clean)
        # 立即派发：AI 刚判定可用的候选不该再排 60 秒的队。_dispatch 只启动
        # 线程就返回，不阻塞 Worker 的 HTTP 请求（它有超时）。
        dispatched = _dispatch(keys=added) if added else []
        _audit(root, "ingest_issues", f"{len(clean)} items",
               f"queued={queued} dispatched={len(dispatched)} "
               f"repos={body.get('repos_scanned', '?')}")
        return JSONResponse({"queued": queued, "received": len(clean),
                             "dispatched": dispatched})

    @app.post("/api/dashboard/settings")
    def api_dashboard_settings(request: Request,
                               cf_pull_seconds: int = Form(20),
                               pr_refresh_seconds: int = Form(120),
                               gh_cache_ttl: int = Form(300)):
        cfg = {"cf_pull_seconds": max(10, min(300, cf_pull_seconds)),
               "pr_refresh_seconds": max(30, min(3600, pr_refresh_seconds)),
               "gh_cache_ttl": max(30, min(3600, gh_cache_ttl)),
               "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        _atomic_write_text(root / "configs" / "dashboard.yaml",
                           yaml.safe_dump(cfg, allow_unicode=True, sort_keys=True))
        _audit(root, "dashboard_settings", "configs/dashboard.yaml",
               f"pull={cfg['cf_pull_seconds']}s pr={cfg['pr_refresh_seconds']}s ttl={cfg['gh_cache_ttl']}s")
        return RedirectResponse("/quota", status_code=303)

    @app.post("/api/health/refresh")
    def api_health_refresh():
        """重跑 health_check.py（读仓库生态 + CONTRIBUTING 政策闸），后台线程执行。"""
        import threading as _th

        def _run():
            import subprocess as _sp
            try:
                log_dir = root / "state" / "runs"
                log_dir.mkdir(parents=True, exist_ok=True)
                out = log_dir / f"health_{time.strftime('%Y%m%d_%H%M')}_manual.log"
                with out.open("w", encoding="utf-8") as fh:
                    _sp.run([sys.executable, "health_check.py"], cwd=str(root),
                            stdout=fh, stderr=_sp.STDOUT, timeout=1800,
                            env={**__import__("os").environ,
                                 "OSSCOUT_HEALTH_ROOT": str(root)})
                _audit(root, "health_refresh", "health_check.py",
                       f"done -> {out.name}")
            except Exception as e:  # noqa: BLE001
                _audit(root, "health_refresh", "health_check.py", f"fail: {str(e)[:150]}")

        _th.Thread(target=_run, daemon=True).start()
        _audit(root, "health_refresh_requested", "health_check.py", "background started")
        return RedirectResponse("/quota", status_code=303)

    @app.post("/api/cloudflare/pull")
    def api_cloudflare_pull():
        out = D.cloudflare_pull(ds)
        (root / "state" / "cloudflare_last_pull.json").write_text(
            json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "out": out},
                       ensure_ascii=False), encoding="utf-8")
        _audit(root, "cloudflare_pull", "kv", json.dumps(out, ensure_ascii=False)[:150])
        return JSONResponse(out)

    # ---------------- 未注册路径兜底 ----------------
    @app.get("/{rest:path}")
    def get_fallback(rest: str):
        return JSONResponse({"detail": "not found"}, status_code=404)

    @app.post("/{rest:path}")
    @app.put("/{rest:path}")
    @app.delete("/{rest:path}")
    def no_writes(rest: str):
        return JSONResponse({"detail": "unknown write endpoint"}, status_code=501)

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
