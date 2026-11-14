"""Controller API for Worker ingestion, repair dispatch and Feishu integration."""
from __future__ import annotations

from scout_v2.roster import monitored_repo as modifiable_repo

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

from . import data as D

PROJECT_ROOT = Path(__file__).resolve().parent.parent

SESSION_COOKIE = "os_session"
SESSION_TTL = 7 * 24 * 3600


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
           "source": "controller"}
    with (root / "state" / "worklog.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    yaml.safe_load(tmp.read_text(encoding="utf-8"))  # 校验
    os.replace(tmp, path)


def _staged_flush_once(root: Path, cfg: dict) -> dict:
    """Compatibility entrypoint: use the same eligible FIFO and daily ledger."""
    from scout_v2.workers.auto_pipeline import drain_ai_graded
    results = drain_ai_graded(root, limit=5, dry_run=False, rescreen=True)
    return {"processed": len(results), "results": results}


_BITABLE_SYNC_LOCK = threading.Lock()


def _sync_bitables(root: Path, *, ds=None, activity_only=False, activity_cache=None) -> dict:
    """Independent table snapshots; a failed source cannot clear remote data."""
    from scout_v2 import feishu_bitable as fb
    from .console_activity import candidate_activity
    import importlib.util
    if not fb.configured(root):
        return {"configured": False}
    spec = importlib.util.spec_from_file_location("monitor_prs", PROJECT_ROOT / "scripts/monitor_prs.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    results = {}
    tables = [
        ("pr", mod.build_pr_rows, fb.TABLE_NAME, fb.PR_FIELDS, fb.row_to_fields, "pr_table_id", None),
        ("repos", mod.build_repo_rows, fb.REPO_TABLE_NAME, fb.LINK_FIELDS, fb.repo_row_to_fields, "repo_table_id", root / "configs/repositories.yaml"),
        ("scan", mod.build_scan_rows, fb.SCAN_TABLE_NAME, fb.SCAN_FIELDS, fb.scan_row_to_fields, "scan_table_id", root / "state/seen.json"),
        ("candidates", mod.build_candidate_rows, fb.CAND_TABLE_NAME, fb.CAND_FIELDS, fb.cand_row_to_fields, "cand_table_id", root / "queue/candidates.json"),
    ]
    activity = None
    for name, build, table, fields, convert, binding, source in tables:
        tracks_activity = name in {"scan", "candidates"}
        if activity_only and not tracks_activity:
            continue
        try:
            with _BITABLE_SYNC_LOCK:
                # Builders tolerate corrupt/missing files by returning []; validate first.
                if source:
                    raw = source.read_text(encoding="utf-8")
                    yaml.safe_load(raw) if source.suffix == ".yaml" else json.loads(raw)
                if tracks_activity:
                    if activity is None:
                        activity = candidate_activity(ds if ds is not None else D.DataSource(root))
                    running, published, pr_states = activity
                    rows = build(root, running, published, pr_states) if name == "scan" else build(root, running)
                    if activity_only and activity_cache is not None and rows == activity_cache.get(name):
                        continue
                else:
                    rows = build(root)
                if name == "pr" and not rows:
                    results[name] = {"skipped": "no complete GitHub snapshot"}
                    continue
                results[name] = fb.sync_table(root, rows, table_name=table, fields=fields,
                                              to_fields=convert, binding_key=binding,
                                              allow_empty=source is not None)
                if tracks_activity and activity_cache is not None:
                    activity_cache[name] = rows
                _audit(root, "bitable_sync_" + name, table, json.dumps(results[name]))
        except Exception as ex:
            results[name] = {"error": str(ex)[:200]}
            _audit(root, "bitable_sync_error", name, str(ex)[:200])
    return results


def create_app(root: Path | None = None, gh_runner=None,
               auth_enabled: bool = True, password: str | None = None) -> FastAPI:
    root = Path(root) if root else Path(
        os.environ.get("OSSCOUT_DASHBOARD_ROOT", PROJECT_ROOT))
    app = FastAPI(title="oss-scout Controller", openapi_url=None)


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
    _bitable_wake = _thr.Event()
    _intake_handoff: set[str] = set()
    model_lock = _thr.Lock()
    # 真正的修复跑在 GitHub Actions runner 上（VPS 只负责派发、提交
    # workflow_dispatch，不 clone 也不编译），所以这里的上限跟 VPS 算力无关，
    # 只用来防止一次性把几百条一起甩给 Actions 打爆自动化的并发配额。
    # repair job 没有 concurrency 组，可以并行跑，实测 3 条同时在飞。
    _MAX_CONCURRENT = 1

    def _run_one(entry: dict, rescreen: bool = False, immediate: bool = False) -> None:
        key = str(entry.get("id", ""))
        out = {}
        try:
            from scout_v2.workers.auto_pipeline import candidate_pipeline
            out = candidate_pipeline(root, entry, rescreen=rescreen, immediate=immediate)
            _audit(root, "auto_repair", key, json.dumps(out, ensure_ascii=False)[:400])
        except Exception as e:  # noqa: BLE001
            try:
                _audit(root, "auto_repair_error", key, str(e)[:200])
            except Exception:  # noqa: BLE001
                pass
        finally:
            with _inflight_lock:
                _inflight.pop(key, None)
            current = _load_candidate(root, key) or {}
            if out.get("abort") != "dispatcher busy" and current.get("status") != "staged":
                _bitable_wake.set()
                _dispatch()


    def _dispatch(*, rescreen: bool = False, just_screened: str | None = None) -> list[str]:
        """派发候选去修复。

        返回本次新派发的 key 列表。不阻塞：线程启动即返回。

        两条入口（ingest 即时触发 / 定时轮询）都必须过当日额度：2026-10-30
        之前 ingest 那条路径完全绕过额度检查，扫到多少就派多少，一天开出 12 个
        PR。现在统一在这里卡。
        """
        started: list[str] = []
        if os.environ.get("OSSCOUT_DISABLE_AUTO_REPAIR") == "1":
            return started
        try:
            from scout_v2.repo_policy import PolicyEngine
            from scout_v2.state_store import publication_quota_remaining
            from scout_v2.workers.auto_pipeline import expire_staged, open_scheduled_batch, expire_direct_starts
            expire_direct_starts(root)
            expire_staged(root)
            open_scheduled_batch(root)
            with _inflight_lock:
                room = _MAX_CONCURRENT - len(_inflight)
            eng, err = PolicyEngine.load(root)
            if err:
                return started
            left = publication_quota_remaining(root, eng.tz_name, eng.max_claims_per_day)
            if left <= 0:
                from scout_v2.workers.auto_pipeline import _stage_candidates
                queued = json.loads((root / "queue/candidates.json").read_text(encoding="utf-8"))
                _stage_candidates(root, [k for k, e in queued.items()
                                        if e.get("status") == "staged"],
                                  tz_name=eng.tz_name, cap=eng.max_claims_per_day)
                return started
            room = min(room, left)
            if room <= 0:
                return started
            from scout_v2.workers.auto_pipeline import drain_ai_graded
            # Fresh issues first. History needs a scheduled batch, closure refill,
            # or an explicit manual request; ordinary polling cannot enroll it.
            cands = [c["id"] for c in drain_ai_graded(root, limit=room, dry_run=True,
                                                     rescreen=rescreen)]
            for key in cands:
                with _inflight_lock:
                    if len(_inflight) >= _MAX_CONCURRENT:
                        break
                    if key in _inflight or (key in _intake_handoff and key != just_screened):
                        continue
                    from scout_v2.state_store import queue_transaction, business_now
                    with queue_transaction(root) as queue:
                        ent = queue.get(key)
                        if not ent or ent.get("status") != "staged":
                            continue
                        if any(k != key and e.get("direct_start_reserved_at")
                               and e.get("status") == "staged" for k, e in queue.items()):
                            continue
                        ent["direct_start_reserved_at"] = business_now().isoformat()
                        ent = dict(ent)
                    th = _thr.Thread(target=_run_one, args=(ent, rescreen, key == just_screened), daemon=True,
                                     name=f"repair-{key}")
                    _inflight[key] = th
                try:
                    th.start()
                except Exception:
                    with _inflight_lock:
                        _inflight.pop(key, None)
                    from scout_v2.workers.auto_pipeline import release_direct_start
                    release_direct_start(root, key)
                    raise
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
                    from scout_v2.workers.auto_pipeline import recover_pending_publication
                    if recover_pending_publication(root):
                        _bitable_wake.set()
                    cfg = D.dashboard_settings(ds)
                    interval = int(cfg.get("auto_repair_seconds", 60))
                    if interval <= 0:
                        _t.sleep(60)
                        continue
                    # 每分钟发现并消费候选（用户裁定 2026-11-05）：
                    # 名额未满就当场修，满了 drain 内部自动标 staged 留到
                    # 次日凌晨批量取。这条循环独立于 PR 监控（后者每 15min
                    # 且要扫 30+ 个 PR 的所有端点，会把候选消费挤到几小时一次）。
                    _dispatch()
                    _t.sleep(interval)
                except Exception as e:  # noqa: BLE001 — 永不因单次异常停摆
                    try:
                        _audit(root, "auto_repair_error", "-", str(e)[:200])
                    except Exception:  # noqa: BLE001
                        pass
                    _t.sleep(120)

        _thr.Thread(target=_loop, daemon=True).start()

    def _publication_status_loop():
        from scout_v2.workers.auto_pipeline import refresh_publication_status

        def _loop():
            while True:
                try:
                    if refresh_publication_status(root):
                        _bitable_wake.set()
                except Exception as exc:  # noqa: BLE001
                    _audit(root, "publication_status_error", "-", str(exc)[:200])
                time.sleep(30)

        _thr.Thread(target=_loop, daemon=True, name="publication-status").start()

    def _intake_loop():
        """Recoverable background AI screening; request handlers only store input."""
        def _loop():
            from scout_v2.state_store import IssueInbox
            if os.environ.get("OSSCOUT_DISABLE_AUTO_REPAIR") == "1":
                return
            while True:
                inbox = None
                try:
                    inbox = IssueInbox(root)
                    row = inbox.take()
                    if not row:
                        time.sleep(2)
                        continue
                    key, payload, version, attempts = row
                    try:
                        queue = json.loads((root / "queue/candidates.json").read_text(encoding="utf-8"))
                        if key in queue:
                            inbox.finish(key, version)
                            continue
                        item = json.loads(payload)
                        if not modifiable_repo(item["repo"]):
                            inbox.finish(key, version)
                            continue
                        with _inflight_lock:
                            _intake_handoff.add(key)
                        try:
                            _, added = D.ingest_cloudflare_issues(ds, [item], refresh=True)
                            seen = json.loads((root / "state/seen.json").read_text(encoding="utf-8"))
                            rec = seen.get(key) or {}
                            if rec.get("status") == "retry":
                                raise RuntimeError(rec.get("fine_reason") or "AI unavailable")
                            inbox.finish(key, version)
                            if added:
                                _dispatch(just_screened=added[0])
                        finally:
                            with _inflight_lock:
                                _intake_handoff.discard(key)
                    except Exception as ex:
                        inbox.finish(key, version, str(ex), attempts)
                        _audit(root, "intake_retry", key, str(ex)[:200])
                except Exception as ex:
                    try:
                        _audit(root, "intake_loop_error", "-", str(ex)[:200])
                    except Exception:
                        pass
                    time.sleep(5)
                finally:
                    if inbox is not None:
                        inbox.close()
        _thr.Thread(target=_loop, daemon=True, name="issue-screening").start()


    def _bitable_loop():
        def _loop():
            if os.environ.get("OSSCOUT_DISABLE_AUTO_REPAIR") == "1":
                return
            time.sleep(10)
            full_sync_at = 0.0
            activity_cache = {}
            while True:
                _bitable_wake.clear()
                full = time.monotonic() >= full_sync_at
                try:
                    _sync_bitables(root, ds=ds, activity_only=not full, activity_cache=activity_cache)
                    if full:
                        full_sync_at = time.monotonic() + 900
                except Exception as ex:
                    _audit(root, "bitable_sync_error", "-", str(ex)[:200])
                _bitable_wake.wait(30)
        _thr.Thread(target=_loop, daemon=True, name="bitable-sync").start()

    def _model_settings_loop():
        from scout_v2.feishu_models import sync_model_settings

        def _loop():
            while True:
                try:
                    sync_model_settings(root, model_lock)
                except Exception as exc:
                    _audit(root, "bitable_model_settings_error", "models", type(exc).__name__)
                time.sleep(30)

        _thr.Thread(target=_loop, daemon=True, name="bitable-model-settings").start()

    def _cf_poll_loop():
        """从 CF KV 拉取 Worker 发现的**兜底**通道（主通道是 Worker 直接 POST）。

        为什么是 5 分钟而不是 20 秒：每次拉取都要调 KV 的 list 接口，而 list
        是独立计费的配额——免费档只有 1000 次/天。20s 轮询 = 4320 次/天，
        直接把配额打爆（2026-11-04 实录：Cloudflare 429，list 全天不可用）。
        这里的角色只是"Worker POST 失败时的捞回"，5 分钟延迟完全可接受。
        """
        import threading as _th
        import time as _t
        def _loop():
            while True:
                try:
                    D.cloudflare_pull(ds)
                except Exception:  # noqa: BLE001
                    pass
                # 默认 300s = 288 次 list/天，占 list 配额不到三成
                _t.sleep(D.cf_pull_seconds(ds))
        _th.Thread(target=_loop, daemon=True).start()

    ds = D.DataSource(root, gh_runner=gh_runner)

    def _reap_orphan_claim():
        from scout_v2.repo_policy import PolicyEngine
        from scout_v2.state_store import business_now, queue_transaction, ClaimLedger
        policy, _ = PolicyEngine.load(root)
        ledger = ClaimLedger(root / "state/scout_v2.sqlite3")
        try:
            ledger.reconcile_legacy_starts(root / "state/worklog.jsonl", "Asia/Shanghai")
            previously_started = {r[0] for r in ledger._conn.execute("SELECT DISTINCT candidate_id FROM claim_days")}
        finally:
            ledger.close()
        # A runner may still publish after the controller restarts. Do not resubmit it.
        with queue_transaction(root) as q:
            for key, entry in q.items():
                if entry.get("status") == "running" or (key in previously_started and entry.get("status") in (
                        "new", "recheck_failed", "staged")):
                    entry.update(status="escalated", abort_reason="controller restarted; inspect remote run before retry")
                    _audit(root, "orphan_requires_review", key, "daily start retained")
                elif entry.get("status") in ("new", "recheck_failed", "staged"):
                    repo = entry.get("repo") or key.split("#", 1)[0]
                    reason = policy.repo_entry(repo).get("review_reason")
                    if reason or not modifiable_repo(repo):
                        reason = reason or "仓库已移出监控名单"
                        entry.update(status="coarse_blocked", coarse_reason=reason,
                                     updated_at=business_now().isoformat(timespec="seconds"))
                        _audit(root, "candidate_roster_block", key, reason)

    if os.environ.get("OSSCOUT_DISABLE_AUTO_REPAIR") != "1":
        _reap_orphan_claim()
        for key in D.prune_candidate_backlog(root):
            _audit(root, "candidate_pruned", key, "超出候选列表上限")
        _cf_poll_loop()
        _intake_loop()
        _auto_repair_loop()
        _publication_status_loop()
        _bitable_loop()
        _model_settings_loop()
    app.state.ds = ds
    app.state.root = root
    secret = _secret(root)
    passwd = password or _dashboard_password(root)

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        path = request.url.path
        admin_route = path.startswith("/api/") or path == "/logout"
        machine_route = path.startswith("/api/ingest/")
        if not auth_enabled or not admin_route or machine_route:
            return await call_next(request)
        token = request.cookies.get(SESSION_COOKIE, "")
        if _check_session(secret, token):
            return await call_next(request)
        return JSONResponse({"detail": "unauthenticated"}, status_code=401)

    @app.get("/healthz")
    def health():
        return {"ok": True}

    @app.post("/login")
    def login(body: dict):
        supplied = body.get("password")
        if not isinstance(supplied, str) or not hmac.compare_digest(supplied.encode(), passwd.encode()):
            time.sleep(1.0)  # 简单防爆破
            return JSONResponse({"detail": "密码错误"}, status_code=401)
        resp = JSONResponse({"ok": True})
        resp.set_cookie(SESSION_COOKIE, _make_session(secret),
                        max_age=SESSION_TTL, httponly=True, samesite="lax")
        _audit(root, "login", "admin-api", "login ok")
        return resp

    @app.post("/logout")
    def logout():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(SESSION_COOKIE)
        return resp

    @app.get("/")
    @app.get("/console/pr-table")
    def console_pr_table():
        from scout_v2.feishu_bitable import pr_table_url
        url = pr_table_url(root)
        if not url:
            return JSONResponse({"detail": "现有 PR 记录表还没有绑定。"}, status_code=503)
        return RedirectResponse(url, status_code=303)

    @app.post("/api/console/schedule")
    def console_schedule(body: dict):
        import datetime as dt
        import re
        from scout_v2.state_store import business_now, queue_transaction

        value = str(body.get("time") or "")
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            return JSONResponse({"detail": "请输入有效的北京时间。"}, status_code=400)
        now = business_now()
        hour, minute = map(int, value.split(":"))
        path = root / "configs/dashboard.yaml"
        with queue_transaction(root) as queue:
            cfg = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
            cfg = cfg or {}
            cfg.update(staged_flush_hour_local=hour, staged_flush_minute_local=minute)
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(path, yaml.safe_dump(cfg, allow_unicode=True))
            for entry in queue.values():
                if (entry.get("status") != "staged" or entry.get("defer_reason") != "daily_quota"
                        or not entry.get("eligible_at")):
                    continue
                try:
                    due = dt.datetime.fromisoformat(entry["eligible_at"])
                    due = due.astimezone(now.tzinfo) if due.tzinfo else due.replace(tzinfo=now.tzinfo)
                except (ValueError, TypeError):
                    continue
                if due > now:
                    entry["eligible_at"] = due.replace(hour=hour, minute=minute).isoformat()
        _audit(root, "console_schedule", "queue", value)
        return {"time": value}

    @app.get("/api/console/models")
    def console_models():
        from scout_v2.console_models import public_models

        return JSONResponse(public_models(root), headers={"Cache-Control": "no-store"})

    @app.post("/api/console/models")
    def console_save_models(body: dict):
        from scout_v2.console_models import model_setting_error, read_models, rotate_opencode_key, write_models

        fields = ("repair_model", "reply_model", "codiv_base", "codiv_model", "opencode_key", "codiv_key")
        if any(not isinstance(body.get(key, ""), str) or len(body.get(key, "")) > 512 for key in fields):
            return JSONResponse({"detail": "模型或密钥字段格式有误。"}, status_code=400)
        values = {key: body.get(key, "").strip() for key in fields}
        for key in ("codiv_base", "repair_model", "reply_model", "codiv_model"):
            error = model_setting_error(key, values[key])
            if error:
                return JSONResponse({"detail": error}, status_code=400)
        with model_lock:
            prior = read_models(root)
            if values["opencode_key"]:
                try:
                    rotate_opencode_key(root, values["opencode_key"])
                except ValueError as exc:
                    return JSONResponse({"detail": str(exc)}, status_code=409)
                except RuntimeError as exc:
                    return JSONResponse({"detail": str(exc)}, status_code=502)
            write_models(root, {**prior, **{k: v for k, v in values.items() if v}})
        _audit(root, "console_models", "providers", "settings updated")
        return {"ok": True}

    @app.get("/api/opencode/models")
    def api_opencode_models():
        from scout_v2.workers.issue_scorer import GoClient, load_scorer_config
        cfg = load_scorer_config(root)
        if not cfg.get("api_key"):
            return JSONResponse({"models": [], "logged_in": False})
        c = GoClient(key=cfg["api_key"], model=cfg.get("model", "deepseek-v4.1-flash"), base_url=cfg.get("base_url"))
        models = c.list_models()
        return JSONResponse({"models": models,
                             "logged_in": bool(models),
                             "current": cfg.get("model", "deepseek-v4.1-flash")})

    @app.get("/api/pipeline/preview")
    def api_pipeline_preview():
        from scout_v2.workers.auto_pipeline import drain_ai_graded
        return JSONResponse({"would_process": drain_ai_graded(root, limit=3, dry_run=True)})

    @app.post("/api/pipeline/run")
    def api_pipeline_run(request: Request):
        return JSONResponse({"dispatched": _dispatch(rescreen=True)})




    @app.post("/api/pr/bitable/sync")
    def api_pr_bitable_sync():
        """手动触发一次多维表格全量同步。"""
        return JSONResponse(_sync_bitables(root, ds=ds))

    # ---------------- 飞书卡片回调（卡片里一键发送） ----------------


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
        coarse = body.get("coarse_rejected")
        if isinstance(coarse, list):
            import datetime as dt
            from scout_v2.state_store import Worklog, business_now
            now = business_now()
            try:
                stamp = dt.datetime.fromisoformat(body["coarse_completed_at"])
                if stamp.tzinfo is None or abs((now - stamp).total_seconds()) > 3600:
                    raise ValueError("invalid telemetry time")
            except (KeyError, TypeError, ValueError):
                return JSONResponse({"detail": "invalid coarse_completed_at"}, status_code=400)
            worklog = Worklog(root / "state/worklog.jsonl")
            reasons = {"excluded_label", "not_open_unassigned_issue"}
            for event in coarse[:2000]:
                if not isinstance(event, dict):
                    continue
                repo, number, reason = (event.get(k) for k in ("repo", "number", "reason"))
                if (isinstance(repo, str) and type(number) is int and number > 0
                        and isinstance(reason, str) and reason in reasons and modifiable_repo(repo)):
                    worklog.append({"ts": stamp.isoformat(), "action": "pipeline_coarse_screen",
                                    "target": f"{repo}#{number}", "result": reason})
            if not items:
                return JSONResponse({"stored": 0})
        # 字段收敛：只保留白名单，防注入超大/异常结构
        clean = []
        for it in items:
            if not isinstance(it, dict):
                continue
            clean.append({
                "repo": str(it.get("repo", ""))[:120],
                "number": int(it.get("number") or 0),
                "title": str(it.get("title", ""))[:400],
                "url": str(it.get("url", ""))[:400],
                "created_at": str(it.get("created_at", ""))[:40],
                "updated_at": str(it.get("updated_at", ""))[:40],
                "issue_comments": it.get("issue_comments") or [],
                "labels": [str(x)[:60] for x in (it.get("labels") or [])][:20],
                "reactions": int(it.get("reactions") or 0),
                "comments": int(it.get("comments") or 0),
                "body_excerpt": str(it.get("body_excerpt", ""))[:65536],
            })
        from scout_v2.state_store import IssueInbox
        inbox = IssueInbox(root)
        try:
            stored = inbox.put(clean)
        finally:
            inbox.close()
        _audit(root, "ingest_issues", f"{len(clean)} items", f"durably stored={stored}")
        return JSONResponse({"queued": stored, "received": len(clean), "dispatched": [], "stored": stored})

    @app.get("/api/ingest/issue-context/{job_id}")
    def api_issue_context(job_id: str, request: Request):
        from fastapi.responses import FileResponse
        import re
        expect = (D.cloudflare_cfg(ds).get("ingest_token") or "").strip()
        if not expect or not hmac.compare_digest(request.headers.get("X-Ingest-Token", ""), expect):
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        if not re.fullmatch(r"job-[0-9a-f-]{36}", job_id):
            return JSONResponse({"detail": "invalid job ID"}, status_code=400)
        path = root / "artifacts" / "issue-context" / f"{job_id}.json"
        if not path.is_file():
            return JSONResponse({"detail": "snapshot unavailable"}, status_code=404)
        return FileResponse(path, media_type="application/json")

    @app.post("/api/ingest/codex-auth")
    async def api_codex_auth(request: Request):
        from scout_v2.codex_auth import exchange
        expect = (D.cloudflare_cfg(ds).get("ingest_token") or "").strip()
        if not expect or not hmac.compare_digest(request.headers.get("X-Ingest-Token", ""), expect):
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        raw = await request.body()
        if len(raw) > 64_000:
            return JSONResponse({"detail": "request too large"}, status_code=413)
        try:
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError()
            response, status = exchange(root, body)
        except ValueError:
            return JSONResponse({"detail": "invalid request"}, status_code=400)
        return JSONResponse(response, status_code=status, headers={"Cache-Control": "no-store"})


    @app.post("/api/cloudflare/pull")
    def api_cloudflare_pull():
        out = D.cloudflare_pull(ds)
        _audit(root, "cloudflare_pull", "kv", json.dumps(out, ensure_ascii=False)[:150])
        return JSONResponse(out)

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8787)
