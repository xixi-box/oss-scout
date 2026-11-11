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

    def _handle_pr_event(root: Path, ev: dict) -> dict:
        from scripts.monitor_prs import dispatch_pr_fix
        repo, number = str(ev["repo"]), int(ev["number"])
        key = f"{repo}#{number}"
        try:
            result = dispatch_pr_fix(
                repo, number, kind="followup", comment_id=ev.get("comment_id"),
                source_kind=ev.get("source_kind"))
            if result.get("dispatched") or ev.get("comment_id"):
                _audit(root, "pr_review_fix_dispatch", key, str(result.get("reason"))[:160])
            return result
        except Exception as exc:
            _audit(root, "pr_event_error", key, str(exc)[:200])
            return {}

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
                    ent = _load_candidate(root, key)
                    if ent is None:
                        continue
                    th = _thr.Thread(target=_run_one, args=(ent, rescreen, key == just_screened), daemon=True,
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

    def _pr_watch_loop():
        """盯着我们开出去的 PR：CI 红了留痕，并把 PR 列表同步给 Worker。

        Playbook §2① 要求"CI 因我们的改动失败 → 读日志、修复、推送"，但这条
        从没实现：PR 开出去之后就没有任何主动动作了。这里每 15 分钟扫一遍
        （比 Actions 的 30 分钟定时更密，两边互为备份）。

        两条职责：
        1. 分类每个 PR（fix_ci / address_review / answer_question），写
           state/pr_watch.jsonl —— 看板和人都读这个。
        2. 把"我们的 PR 列表"同步到 Cloudflare Worker 的 PRS 变量（写到
           /data/state/pr_sync.json，由部署脚本读取）。Worker 靠这个列表轮询
           评论，是评论即时通道的前提——没有列表它不知道该看哪些 PR。

        不自动发评论：维护者提了意见该怎么回需要人判断。
        """
        import time as _t

        def _loop():
            if os.environ.get("OSSCOUT_DISABLE_AUTO_REPAIR") == "1":
                return
            _t.sleep(120)
            while True:
                try:
                    from scout_v2.pr_approval import cleanup
                    cleanup(root)
                except Exception as exc:
                    _audit(root, "pr_approval_cleanup_error", "-", str(exc)[:160])
                try:
                    from scripts import monitor_prs as mod
                    results = mod.run(dry_run=False)

                    # 同步 PR 列表给 Worker（评论通道的输入）。
                    # 两条通道：写 /data/state/pr_sync.json 留作审计，写 CF KV
                    # 让 Worker 真的能读到。KV 免费档 1000 write/天，15 分钟一次
                    # 是 96 次/天，安全。
                    prs = [f"{r['repo']}#{r['number']}" for r in results]
                    try:
                        (root / "state" / "pr_sync.json").write_text(
                            json.dumps({"at": _t.strftime("%Y-%m-%dT%H:%M:%S"),
                                        "prs": prs}, ensure_ascii=False),
                            encoding="utf-8")
                    except OSError:
                        pass
                    try:
                        out = D.cloudflare_kv_put(ds, "prs", json.dumps(prs))
                        if not out.get("ok"):
                            _audit(root, "pr_sync_failed", f"{len(prs)} prs",
                                   str(out.get("error"))[:150])
                    except Exception:  # noqa: BLE001
                        pass

                    for r in results:
                        key = f"{r['repo']}#{r['number']}"
                        action = r.get("action")
                        followup = _handle_pr_event(root, r)
                        if action == "fix_ci" and not followup.get("dispatched"):
                            try:
                                out = mod.dispatch_pr_fix(r["repo"], r["number"])
                                _audit(root, "pr_fix_dispatch", key,
                                       f"dispatched={out.get('dispatched')} reason={out.get('reason')}")
                            except Exception as e:
                                _audit(root, "pr_fix_error", key, str(e)[:200])

                except Exception as e:  # noqa: BLE001
                    try:
                        _audit(root, "pr_watch_error", "-", str(e)[:200])
                    except Exception:  # noqa: BLE001
                        pass
                _t.sleep(900)

        _thr.Thread(target=_loop, daemon=True).start()

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
        _pr_watch_loop()
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
        machine_route = path.startswith("/api/ingest/") or path == "/api/pr/reply/fix-result"
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

    def _post_pr_comment(repo: str, number: int, body: str,
                         kind: str = "issue",
                         comment_id: int | str | None = None) -> tuple[bool, str]:
        """以本账号向 GitHub 发 PR 评论。返回 (ok, detail)。

        kind="inline" 且带 comment_id → 回到那条行内评论的线程里
        （pulls/{n}/comments/{id}/replies）；否则进 issue 层。
        行内发言行内回、行外发言行外回——发错层对话就断线了。
        """
        import subprocess as sp
        if not modifiable_repo(repo):
            return False, "repository outside monitored roster"
        if kind == "inline" and str(comment_id or "").isdigit():
            endpoint = (f"repos/{repo}/pulls/{number}/comments/"
                        f"{comment_id}/replies")
        else:
            endpoint = f"repos/{repo}/issues/{number}/comments"
        try:
            r = sp.run(["gh", "api", endpoint,
                        "-X", "POST", "-f", f"body={body}"],
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=60)
        except (sp.TimeoutExpired, OSError) as e:
            return False, str(e)[:200]
        return r.returncode == 0, (r.stderr or "")[:200]

    def _mark_reply(key: str, status: str, **extra) -> None:
        from scout_v2.state_store import queue_transaction
        rp = root / "state" / "pr_replies.json"
        with queue_transaction(root, path=rp) as replies:
            entry = replies.setdefault(key, {})
            entry.update({"status": status, **extra})

    @app.post("/api/pr/reply/fix-result")
    async def api_pr_reply_fix_result(request: Request):
        """pr-fix workflow 回报结果：改动已提交到暂存分支（未推 PR）。

        鉴权用与 Worker 相同的 X-Ingest-Token——这是内部通道，不接受匿名。
        写入后飞书卡片会从"进行中"变成"可发送"。
        """
        cfg = D.cloudflare_cfg(ds)
        expect = (cfg.get("ingest_token") or "").strip()
        got = request.headers.get("X-Ingest-Token", "").strip()
        if not expect:
            return JSONResponse({"detail": "ingest_token not configured"},
                                status_code=503)
        if not hmac.compare_digest(got, expect):
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        try:
            body = json.loads((await request.body()) or b"{}")
        except Exception:  # noqa: BLE001
            return JSONResponse({"detail": "bad json"}, status_code=400)
        repo = str(body.get("repo") or "").strip()
        num = body.get("number")
        if not repo or not isinstance(num, int):
            return JSONResponse({"detail": "repo/number required"}, status_code=400)
        if not modifiable_repo(repo):
            return JSONResponse({"detail": "repository outside monitored roster"}, status_code=403)
        key = f"{repo}#{num}"
        if body.get("job_id"):
            from scout_v2 import feishu_app, pr_approval
            from scout_v2.state_store import queue_transaction
            from starlette.concurrency import run_in_threadpool

            def deliver():
                if body.get("kind") == "followup":
                    from scripts.monitor_prs import is_human_followup_author
                    trigger = body.get("trigger") or {}
                    if not is_human_followup_author(str(trigger.get("author") or ""), trigger.get("author_type")):
                        _audit(root, "pr_followup_no_approval", key, "non-human trigger")
                        return {"ok": True, "outcome": "no_approval"}
                responses = body.get("responses") or []
                drafts = {item["id"]: str(item.get("draft") or "")[:1000] for item in responses}
                if body.get("preview") is True:
                    if (repo.lower() != "python/cpython" or num != 161543 or not responses
                            or type(body.get("review_id")) is not int):
                        raise ValueError("invalid CPython preview result")
                    card = feishu_app.build_pr_approval_card(
                        {**body, "targets": pr_approval.review_targets(responses)}, drafts=drafts,
                        preview=True, complete=not (body.get("commit_sha") or any(drafts.values())))
                    chat = feishu_app.find_group_chat_id()
                    ok, detail = feishu_app.send_card_to_chat(chat, card, message_uuid=body["job_id"])
                    if not ok:
                        raise RuntimeError(f"Feishu preview delivery failed: {detail}")
                    _audit(root, "pr_review_preview_delivered", key, body["job_id"])
                    return {"ok": True, "outcome": "preview"}
                record = pr_approval.stage(root, body)
                if not record:
                    _audit(root, "pr_followup_no_approval", key, str(body.get("test_result"))[:200])
                    return {"ok": True, "outcome": "no_approval"}
                if not record.get("message_id") and not record.get("complete"):
                    chat = feishu_app.find_group_chat_id()
                    if not chat:
                        raise RuntimeError("Feishu approval chat unavailable")
                    ok, detail = feishu_app.send_card_to_chat(
                        chat, feishu_app.build_pr_approval_card(
                            record, draft=str(body.get("reply") or "").strip()[:1000], drafts=drafts),
                        message_uuid=record["job_id"])
                    if not ok or not detail:
                        raise RuntimeError(f"Feishu approval delivery failed: {detail}")
                    path, _ = pr_approval._paths(root, record["job_id"])
                    with queue_transaction(root, path=path, backup=False) as stored:
                        if stored:
                            stored.update(message_id=detail, chat_id=chat)
                    _audit(root, "pr_approval_ready", key, record["job_id"])
                return {"ok": True}

            try:
                return JSONResponse(await run_in_threadpool(deliver))
            except ValueError as exc:
                return JSONResponse({"detail": str(exc)}, status_code=400)
            except Exception as exc:
                _audit(root, "pr_approval_delivery_error", key, str(exc)[:200])
                return JSONResponse({"detail": "approval delivery failed"}, status_code=502)
        # no_change = opencode 判断"无需变更"（修复已在分支上/意见已被
        # 覆盖）。这不是失败——必须落账并记入去重表，否则 15 分钟监控
        # 循环会对同一条 PR 反复派发、反复给用户发同一张卡片
        # （2026-11-05 实录：5 条已修复的 PR 被循环重派了 3 小时）。
        if body.get("no_change"):
            extra = {"fix_state": "nochange",
                     "fix_test_result": "no changes (already addressed)",
                     "fix_done_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
            _mark_reply(key, "draft", **extra)
            _audit(root, "pr_fix_result", key, "no_change")
            return JSONResponse({"ok": True, "outcome": "no_change"})

        pushed = body.get("pushed") is True and bool(body.get("pushed_sha"))
        extra = {"fix_state": "pushed" if pushed else "failed",
                 "fix_test_result": str(body.get("test_result") or "")[:200],
                 "fix_changed": [str(c)[:120] for c in
                                 (body.get("changed_files") or [])][:10],
                 "fix_done_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        if body.get("pushed_sha"):
            extra["fix_sha"] = str(body["pushed_sha"])
        if body.get("fork_repo"):
            extra["fix_fork"] = str(body["fork_repo"])
        _mark_reply(key, "draft", **extra)
        _audit(root, "pr_fix_result", key,
               f"pushed={pushed} test={str(body.get('test_result'))[:80]}")
        return JSONResponse({"ok": True})

    @app.post("/api/pr/bitable/sync")
    def api_pr_bitable_sync():
        """手动触发一次多维表格全量同步。"""
        return JSONResponse(_sync_bitables(root, ds=ds))

    # ---------------- 飞书卡片回调（卡片里一键发送） ----------------

    @app.post("/feishu/callback")
    async def feishu_callback(request: Request):
        """飞书卡片动作回调：回复草稿卡片里的「一键发送」走这里。

        URL 验证（首次配置回调时飞书发的 challenge）在这里原样回显；
        表单提交（form_submit）解析编辑后的 reply_text，以本账号发到 GitHub，
        并把草稿标记为 sent。verification token 用于校验请求确实来自飞书。
        """
        try:
            body = json.loads((await request.body()) or b"{}")
        except Exception:  # noqa: BLE001
            return JSONResponse({"detail": "bad json"}, status_code=400)

        # 1) 回调地址验证：飞书要求原样回显 challenge
        if body.get("type") == "url_verification":
            uv = body.get("url_verification") or body
            return JSONResponse({"challenge": uv.get("challenge")})

        # 2) 校验来源：卡片回调的验证 token 在 header.token。
        #    注意 event.token 是"更新卡片用的凭证"（30 分钟、限 2 次更新），
        #    不是验证 token——早期实现拿它比对，导致每个真实点击都 403
        #    （2026-11-04 实录：飞书确实打进来了，日志里三次 403）。
        #    fail-closed：token 未配置时拒绝所有卡片动作——这个端点能以我们
        #    名义发 GitHub 评论，不能在未设防的状态下暴露在公网。
        header = body.get("header") or {}
        event = body.get("event") or {}
        token_in = (header.get("token") or body.get("token")
                    or event.get("token") or "")
        want = (os.environ.get("FEISHU_VERIFICATION_TOKEN")
                or D.feishu_verification_token(ds) or "")
        if not want:
            _audit(root, "feishu_cb_reject", "-", "verification token not configured")
            return JSONResponse({"detail": "callback verification not configured"},
                                status_code=503)
        if token_in != want:
            _audit(root, "feishu_cb_reject", "-",
                   f"token mismatch (len in={len(str(token_in))} "
                   f"want={len(str(want))})")
            return JSONResponse({"detail": "forbidden"}, status_code=403)

        # 3) 解析 form_submit：卡片 form 里的 reply_text + 按钮带的 repo/number
        action = (event.get("action") or {})
        form_value = action.get("form_value") or {}
        value = action.get("value") or {}
        if value.get("do") == "preview_pr_followup":
            return JSONResponse({"toast": {"type": "info", "content": "演示成功，未向 GitHub 写入"}})
        if value.get("do") == "approve_pr_followup":
            import sqlite3
            from scout_v2 import feishu_app, pr_approval
            job_id = str(value.get("job_id") or "")
            context = event.get("context") or {}
            msg_id = str(context.get("open_message_id") or (body.get("context") or {}).get("open_message_id")
                         or body.get("open_message_id") or "")
            try:
                path, _ = pr_approval._paths(root, job_id)
                record = json.loads(path.read_text(encoding="utf-8"))
                expires = record.get("expires_at") or path.stat().st_mtime + pr_approval.RETENTION_SECONDS
            except (ValueError, OSError):
                return JSONResponse({"toast": {"type": "error", "content": "审批记录已过期或不存在"}})
            operator = (event.get("operator") or {}).get("open_id") or body.get("open_id")
            allowed = os.environ.get("FEISHU_PR_APPROVER_OPEN_ID", "").strip()
            if (not msg_id or msg_id != record.get("message_id")
                    or not operator or (allowed and operator != allowed)):
                return JSONResponse({"toast": {"type": "error", "content": "无法验证审批人或卡片"}}, status_code=403)
            edited = form_value.get("reply_text")
            drafts, selected = {}, []
            submitted = edited if isinstance(edited, str) else None
            if time.time() >= expires:
                return JSONResponse({"toast": {"type": "error", "content": "审批已过期（保留 7 天）"}})
            if record.get("source_author"):
                from scripts.monitor_prs import is_human_followup_author
                if not is_human_followup_author(record["source_author"]):
                    return JSONResponse({"toast": {"type": "info", "content": "这是机器人通知，无需发布"}})
            if not record.get("complete") and record.get("has_reply", bool(record.get("reply"))):
                if not isinstance(edited, str) or (not record.get("commit_sha") and not edited.strip()):
                    return JSONResponse({"toast": {"type": "error", "content": "请确认卡片已填写并提交回复"}})
            if record.get("targets"):
                fields = {target["id"]: f"reply_{i}" for i, target in enumerate(record["targets"])
                          if target["has_draft"]}
                drafts = {target: form_value[field] for target, field in fields.items()
                          if isinstance(form_value.get(field), str)}
                selected = form_value.get("selected_replies") or []
                if (not isinstance(selected, list) or any(
                        not isinstance(target, str) or not drafts.get(target, "").strip() for target in selected)
                        or (not selected and not record.get("commit_sha") and not record.get("complete"))):
                    return JSONResponse({"toast": {"type": "error", "content": "请选择要发送的回复并填写内容"}})
                submitted = {target: drafts[target] for target in selected}

            def approve():
                key = f"{record['repo']}#{record['number']}"
                try:
                    done = pr_approval.publish(root, job_id, submitted, _post_pr_comment)
                    if done.get("already_complete"):
                        return
                    if done.get("pushed"):
                        _mark_reply(key, "sent" if done.get("reply_status") == "sent" else "completed",
                                    fix_state="pushed", fix_sha=done["commit_sha"], fix_fork=done["fork_repo"])
                        _audit(root, "pr_fix_result", key, f"pushed=True commit={done['commit_sha']}")
                    if done.get("reply_status") == "sent":
                        _audit(root, "pr_reply_sent", key, job_id)
                    elif done.get("reply_status") == "failed":
                        _audit(root, "pr_reply_failed", key, job_id)
                    card = feishu_app.build_pr_approval_card(
                        done, complete=True, draft=(edited or "").strip()[:1000], drafts=drafts)
                except sqlite3.OperationalError as exc:
                    if "locked" in str(exc):
                        return
                    _audit(root, "pr_approval_failed", key, str(exc)[:200])
                    card = feishu_app.build_pr_approval_card(
                        record, draft=(edited or "").strip()[:1000], drafts=drafts, selected=selected,
                        error="确认未完成，请稍后重试")
                except Exception as exc:
                    _audit(root, "pr_approval_failed", key, str(exc)[:200])
                    card = feishu_app.build_pr_approval_card(
                        record, draft=(edited or "").strip()[:1000], drafts=drafts, selected=selected,
                        error=f"确认未完成：{exc}")
                feishu_app.update_card_message(msg_id, card)

            if record.get("complete"):
                return JSONResponse({"toast": {"type": "info", "content": "这张卡片已处理，请查看卡片结果"}})
            _thr.Thread(target=approve, daemon=True).start()
            return JSONResponse({"toast": {"type": "info", "content": "已收到确认，完成后会更新这张卡片"}})
        repo = str(value.get("repo") or form_value.get("repo") or "").strip()
        num_s = str(value.get("number") or form_value.get("number") or "").strip()
        reply_text = str(form_value.get("reply_text")
                         or value.get("reply_text") or "").strip()[:2000]
        if not repo or not num_s.isdigit() or not reply_text:
            return JSONResponse({"detail": "missing repo/number/reply_text"},
                                status_code=400)
        number = int(num_s)
        key = f"{repo}#{number}"
        reply_kind = str(value.get("kind") or "issue")
        reply_cid = str(value.get("comment_id") or "")

        # 代码修改已经在 pr-fix 那一步自动推送过了，这里只负责发回复。
        # 人审的边界是"对外文字"，不是代码——但回复里若声称改了什么，
        # 那个改动必须真的已经在 PR 上，否则就是撒谎。fix_state 只用于
        # 卡片展示，不阻塞发送。
        ok, detail = _post_pr_comment(repo, number, reply_text,
                                      kind=reply_kind, comment_id=reply_cid)
        if not ok:
            _audit(root, "feishu_cb_send_failed", key, detail)
            return JSONResponse({"toast": {"type": "error",
                                           "content": "发送失败：" + detail[:80]}})
        _mark_reply(key, "sent", sent_body=reply_text,
                    sent_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
                    sent_kind=reply_kind if reply_kind == "inline" else "issue",
                    sent_comment_id=reply_cid or None)
        _audit(root, "feishu_cb_sent", key,
               f"kind={reply_kind} cid={reply_cid or '-'} " + reply_text[:160])
        # 回传新卡片：把表单替换成"已发送"确认。
        # 飞书要求 card 包一层 {"type": "raw", "data": {...}}；直接返回裸
        # card 对象不会更新卡片，点击后仍停在旧的输入框上。
        from scout_v2.feishu_app import build_sent_card, update_card_message
        sent_card = build_sent_card(repo, number, reply_text)
        # 除了回传，再主动 PATCH 一次原消息。回调响应若超时或丢失，
        # 卡片会一直停在输入框状态，人不知道到底发没发出去。
        msg_id = str(body.get("open_message_id")
                     or (body.get("event") or {}).get("context", {})
                     .get("open_message_id") or "")
        if msg_id:
            ok_upd, err = update_card_message(msg_id, sent_card)
            _audit(root, "feishu_card_update", key,
                   "ok" if ok_upd else f"failed: {err[:120]}")
        return JSONResponse({
            "toast": {"type": "success", "content": "已发送 ✓"},
            "card": {"type": "raw", "data": sent_card},
        })

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

    @app.post("/api/ingest/pr-comments")
    async def api_ingest_pr_comments(request: Request):
        """Worker 推过来的 PR 新评论（我们自己开出去的 PR）。

        为什么走这条路而不是 GitHub 的 webhook：webhook 注册在**仓库**上，要接收
        上游仓库（mypy、milvus 等）的事件需要那个仓库的 admin 权限，我们没有。
        Worker 每 60 秒拉一次 comments?since=...，命中就推到这里。

        这里只做**记录和分类**，不发任何对外文字：维护者提了意见该怎么回，是
        需要人判断的事（astral-sh 明文禁止 AI 写沟通，biome 说低信号描述会被
        直接关）。分类结果进 state/pr_watch.jsonl，看板和 PR 监控循环都读它。
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

        raw = body.get("events")
        if not isinstance(raw, list):
            return JSONResponse({"detail": "events must be a list"}, status_code=400)

        # 按 comment_id 去重：Worker 的窗口有 10 分钟重叠，同一条会被推到多次
        seen_path = root / "state" / "pr_comments.json"
        try:
            seen = json.loads(seen_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            seen = {}
        watch_path = root / "state" / "pr_watch.jsonl"
        from scripts.monitor_prs import _classify_by_rules, is_human_followup_author

        fresh = 0
        for ev in raw[:200]:
            if not isinstance(ev, dict):
                continue
            cid = str(ev.get("comment_id") or "")
            repo = str(ev.get("repo", ""))[:120]
            try:
                num = int(ev.get("number") or 0)
            except (TypeError, ValueError):
                continue
            if not cid or not repo or not num or cid in seen or not modifiable_repo(repo):
                continue
            seen[cid] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                         "repo": repo, "number": num}
            author = str(ev.get("author") or "")
            comment = str(ev.get("body") or "")
            human = is_human_followup_author(author)
            action = _classify_by_rules(comment, {"author": {"login": author}})[0] if human else "wait"
            rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "source": "worker",
                   "repo": repo, "number": num, "action": action,
                   "author": str(ev.get("author", ""))[:60],
                   "excerpt": str(ev.get("body", ""))[:300]}
            watch_path.parent.mkdir(parents=True, exist_ok=True)
            with watch_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fresh += 1

            # 即时处理：与 15 分钟扫描循环相同的处理链（分类→派发修复→
            # 起草回复→飞书通知）。Worker 每分钟推一次，这条通道让维护者
            # 意见的响应延迟从"最多 15 分钟"压到"约 1 分钟"。
            # 不在 HTTP 请求里做（可能烧几分钟 AI/Actions）——后台线程。
            if cid.isdigit() and human and comment.strip():
                _thr.Thread(target=_handle_pr_event, args=(root, {
                    "repo": repo, "number": num, "comment_id": int(cid), "source_kind": "issue",
                }), daemon=True).start()

        if fresh:
            tmp = seen_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(seen, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, seen_path)
            _audit(root, "pr_comment", f"{fresh} new",
                   ", ".join(f"{r['repo']}#{r['number']}" for r in
                             list(seen.values())[-3:]))
        return JSONResponse({"received": len(raw), "new": fresh})

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
