"""oss-scout v2 Web Dashboard（登录 + 只读监控 + 受审计的配置写入）。

启动：python -m dashboard.app  → http://127.0.0.1:8787
安全：登录会话（HMAC 签名 cookie）；全 Jinja2 自动转义；写端点全部过认证、
校验并逐条写 worklog 审计；日志查看有路径校验；token 打码。
密码来源：环境变量 DASHBOARD_PASSWORD 优先，否则自动生成
state/dashboard_password.txt（gitignore，首启时展示一次）。
"""
from __future__ import annotations

from scout_v2.roster import monitored_repo as modifiable_repo

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

# 看板公网入口（飞书卡片按钮跳转用），与 scout_v2.notify 保持同一来源
NOTIFY_DASHBOARD_URL = (os.environ.get("DASHBOARD_PUBLIC_URL")
                        or "https://oss.wangshun.work").rstrip("/")

PROJECT_ROOT = Path(__file__).resolve().parent.parent

BASE_DIR = Path(__file__).resolve().parent
templates_dir = BASE_DIR / "templates"
static_dir = BASE_DIR / "static"

SESSION_COOKIE = "os_session"
SESSION_TTL = 7 * 24 * 3600

VALID_LANES = ("open", "gated", "restricted", "observe")
VALID_MODES = ("scan", "reproduce", "repair", "review", "ci")
VALID_BACKENDS = ("modal", "actions", "")
# "" 作为 fallback 是合法值：2026-10-31 分工独占后，fallback 一律为空（互不兜底）
# bug_hunter_light / bug_hunter_deep_modal 已删（2026-10-31）：设置进 yaml 也
# 没有任何代码读它们，是能写不能用的死开关。bug hunter 的开关就是
# configs/v2.yaml 的 budget.modal.enabled。
VALID_FEATURES = ("actions_backend", "opencode_models")


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


def _staged_flush_once(root: Path, cfg: dict) -> dict:
    """Compatibility entrypoint: use the same eligible FIFO and daily ledger."""
    from scout_v2.workers.auto_pipeline import drain_ai_graded
    results = drain_ai_graded(root, limit=5, dry_run=False, rescreen=True)
    return {"processed": len(results), "results": results}


def _sync_bitables(root: Path) -> dict:
    """Independent table snapshots; a failed source cannot clear remote data."""
    from scout_v2 import feishu_bitable as fb
    import importlib.util
    if not fb.configured(root):
        return {"configured": False}
    spec = importlib.util.spec_from_file_location("monitor_prs", PROJECT_ROOT / "scripts/monitor_prs.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    results = {}
    tables = [
        ("pr", mod.build_pr_rows, fb.TABLE_NAME, fb.FIELDS, fb.row_to_fields, "pr_table_id", None),
        ("repos", mod.build_repo_rows, fb.REPO_TABLE_NAME, fb.REPO_FIELDS, fb.repo_row_to_fields, "repo_table_id", root / "configs/repositories.yaml"),
        ("scan", mod.build_scan_rows, fb.SCAN_TABLE_NAME, fb.SCAN_FIELDS, fb.scan_row_to_fields, "scan_table_id", root / "state/seen.json"),
        ("candidates", mod.build_candidate_rows, fb.CAND_TABLE_NAME, fb.CAND_FIELDS, fb.cand_row_to_fields, "cand_table_id", root / "queue/candidates.json"),
    ]
    for name, build, table, fields, convert, binding, source in tables:
        try:
            # Builders tolerate corrupt/missing files by returning []; validate first.
            if source:
                raw = source.read_text(encoding="utf-8")
                yaml.safe_load(raw) if source.suffix == ".yaml" else json.loads(raw)
            rows = build(root)
            if name == "pr" and not rows:
                results[name] = {"skipped": "no complete GitHub snapshot"}
                continue
            results[name] = fb.sync_table(root, rows, table_name=table, fields=fields,
                                          to_fields=convert, binding_key=binding,
                                          allow_empty=source is not None)
            _audit(root, "bitable_sync_" + name, table, json.dumps(results[name]))
        except Exception as ex:
            results[name] = {"error": str(ex)[:200]}
            _audit(root, "bitable_sync_error", name, str(ex)[:200])
    return results


def _notify_draft(root: Path, r: dict, draft: dict) -> None:
    """把"AI 草稿待人审"推给飞书，人可在卡片里改完一键发。

    卡片的状态取自 state/pr_replies.json 的 fix_state：
      无 / "none"  纯回复，按钮只发评论
      "pending"    改动在流水线里跑，按钮会等结果
      "ready"      改动已提交到暂存分支，按钮发评论 + 推送

    优先走自建应用卡片（含可编辑输入框 + form_submit 按钮，回调
    /feishu/callback 直接发 GitHub）；应用未配置时回退到 webhook 卡片，
    按钮只能跳看板——自定义机器人没有应用身份，收不到按钮点击。
    通知失败绝不影响主流程。
    """
    repo, number = str(r.get("repo") or ""), int(r.get("number") or 0)
    key = f"{repo}#{number}"
    entry = draft.get("entry") or {}
    text = str(entry.get("draft") or draft.get("draft") or "")
    if not text:
        return

    fix_state = "none"
    try:
        stored = json.loads((root / "state" / "pr_replies.json")
                            .read_text(encoding="utf-8")).get(key) or {}
        fix_state = str(stored.get("fix_state") or "none")
    except (OSError, json.JSONDecodeError):
        pass

    pr_url = str(r.get("pr_url") or entry.get("pr_url")
                 or f"https://github.com/{repo}/pull/{number}")
    try:
        from scout_v2 import feishu_app

        if feishu_app.app_configured():
            chat = feishu_app.find_group_chat_id()
            if chat:
                card = feishu_app.build_reply_card(
                    repo=repo, number=number,
                    issue_title=str(r.get("pr_title") or f"{repo}#{number}"),
                    source_author=str(r.get("latest_author") or "unknown"),
                    source_comment=str(r.get("latest_body")
                                       or entry.get("source_comment") or ""),
                    root_cause=str(entry.get("root_cause") or ""),
                    draft=text,
                    fix_state=fix_state,
                    pr_url=pr_url,
                    source_kind=str(entry.get("source_kind") or "issue"),
                    comment_id=entry.get("source_comment_id"))
                ok, err = feishu_app.send_card_to_chat(chat, card)
                _audit(root, "feishu_card", key,
                       f"fix_state={fix_state} "
                       + ("sent" if ok else f"failed: {err[:150]}"))
                if ok:
                    return
        from scout_v2.notify import feishu_send_card

        state = {"ready": "✅ 已修改并提交（待你确认后推送）",
                 "pending": "⏳ 代码修改进行中",
                 }.get(fix_state, "💬 回复草稿待人审")
        feishu_send_card(
            f"{state}：{key}",
            f"**来自** {r.get('latest_author') or '?'}："
            f"{str(r.get('latest_excerpt'))[:150]}\n"
            "**AI 草稿**：\n" + text[:600] +
            "\n\n→ 应用未接入，点按钮去看板编辑发送",
            button_text="打开 PR 管理页",
            button_url=f"{NOTIFY_DASHBOARD_URL}/prs",
            template="orange")
    except Exception as e:  # noqa: BLE001 — 通知永远不影响主流程
        try:
            _audit(root, "feishu_card_error", key, str(e)[:200])
        except Exception:  # noqa: BLE001
            pass


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
    routing 块内的行注（可接受：语义由本函数与测试保证），其余注释原样保留。

    fallback="" 渲染成带引号的 `""`：裸 `fallback: ` 会被 safe_load 读回 None。
    路由层把 falsy fallback 都视为"无兜底"，None 碰巧行为一致，但写进去的类型
    读回来变了属于静默变形，显式引号防患。
    """
    p = root / "configs" / "v2.yaml"
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    routing = data.get("routing") or {}
    routing[mode] = {"primary": primary, "fallback": fallback}
    lines = ["routing:"]
    for m in ("scan", "reproduce", "repair", "review", "ci"):
        e = routing.get(m) or {"primary": "modal", "fallback": "actions"}
        fb = e.get("fallback") or '""'
        lines.append(f"  {m}:      {{primary: {e['primary']},   fallback: {fb}}}")
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

    # 看板公网入口（飞书通知按钮跳转用）
    DASHBOARD_URL = (os.environ.get("DASHBOARD_PUBLIC_URL")
                     or "https://oss.wangshun.work").rstrip("/")

    # ---- 修复派发器（ingest 与轮询共用）----
    # pipeline 是长任务（几十分钟），不能让 HTTP 请求或轮询等它：
    # 每条候选一个 daemon 线程，inflight 表 + 锁防重复派发。
    import threading as _thr
    _inflight: dict[str, object] = {}
    _inflight_lock = _thr.Lock()
    # 真正的修复跑在 GitHub Actions runner 上（VPS 只负责派发、提交
    # workflow_dispatch，不 clone 也不编译），所以这里的上限跟 VPS 算力无关，
    # 只用来防止一次性把几百条一起甩给 Actions 打爆自动化的并发配额。
    # repair job 没有 concurrency 组，可以并行跑，实测 3 条同时在飞。
    _MAX_CONCURRENT = 1

    def _run_one(entry: dict, rescreen: bool = False) -> None:
        key = str(entry.get("id", ""))
        out = {}
        try:
            from scout_v2.workers.auto_pipeline import candidate_pipeline
            out = candidate_pipeline(root, entry, rescreen=rescreen)
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
            if out.get("abort") != "dispatcher busy" and current.get("status") not in (
                    "new", "queued", "recheck_failed", "staged"):
                _dispatch(rescreen=rescreen)

    def _publications_left() -> int:
        """Number of new PRs that may still be published today."""
        try:
            from scout_v2.repo_policy import PolicyEngine
            from scout_v2.state_store import ClaimLedger
            eng, err = PolicyEngine.load(root)
            if err:
                return 0
            cap = int(getattr(eng, "max_claims_per_day", 0) or 0)
            if cap <= 0:
                return 10 ** 6
            led = ClaimLedger(root / "state" / "scout_v2.sqlite3")
            try:
                used = led.published_today(eng.tz_name)
            finally:
                led.close()
            return max(0, cap - used)
        except Exception:  # noqa: BLE001 — 额度算不出来就当作没额度，宁可不做
            return 0

    def _handle_pr_event(root: Path, ev: dict) -> None:
        """Worker 即时推来的 PR 评论 → 与扫描循环相同的处理链。

        在后台线程运行（可能烧几分钟 AI/Actions），失败只写审计。
        处理链：加载 monitor_prs → 构造 classify 输入 → address_review /
        answer_question / fix_ci 分派 → 起草回复 → 飞书卡片。
        去重：dispatch 靠 review digest，draft 靠 for_comment——与扫描
        循环共用一套机制，不会重复。
        """
        try:
            repo = str(ev.get("repo") or "")
            num = int(ev.get("number") or 0)
            if not repo or not num or not modifiable_repo(repo):
                return
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "monitor_prs",
                Path(__file__).resolve().parents[1] / "scripts" / "monitor_prs.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            key = f"{repo}#{num}"
            state = mod.pr_state(repo, num)
            state.update({"repo": repo, "number": num})
            cls = mod.classify(state)
            action = cls.get("action", "wait")
            _audit(root, "pr_event_classified", key,
                   f"{action} reason={str(cls.get('reason'))[:100]}")
            if action == "address_review":
                fix_out = mod.dispatch_pr_fix(
                    repo, num, kind="review",
                    review_comment=str(state.get("latest_body") or ""),
                    review_author=str(state.get("latest_author") or ""))
                _audit(root, "pr_review_fix_dispatch", key,
                       f"dispatched={fix_out.get('dispatched')} "
                       f"reason={str(fix_out.get('reason'))[:120]}")
                if fix_out.get("dispatched"):
                    _mark_reply(key, "draft", fix_state="pending",
                                fix_fork=str(fix_out.get("fork_repo") or ""))
            elif action == "answer_question":
                draft = mod.draft_reply(repo, num, context=state)
                _audit(root, "pr_reply_drafted", key,
                       f"drafted={draft.get('drafted')} "
                       f"reason={str(draft.get('reason'))[:80]}")
                if draft.get("drafted"):
                    _notify_draft(root, state, draft)
        except Exception as e:  # noqa: BLE001 — 通知永不影响主流程
            try:
                _audit(root, "pr_event_error",
                       f"{ev.get('repo')}#{ev.get('number')}", str(e)[:200])
            except Exception:  # noqa: BLE001
                pass

    def _dispatch(keys: list[str] | None = None, limit: int = 8, *, rescreen: bool = False) -> list[str]:
        """派发候选去修复。keys 为空时从队列按 AI 判定挑。

        返回本次新派发的 key 列表。不阻塞：线程启动即返回。

        两条入口（ingest 即时触发 / 定时轮询）都必须过当日额度：2026-10-30
        之前 ingest 那条路径完全绕过额度检查，扫到多少就派多少，一天开出 12 个
        PR。现在统一在这里卡。
        """
        started: list[str] = []
        if os.environ.get("OSSCOUT_DISABLE_AUTO_REPAIR") == "1":
            return started
        try:
            from scout_v2.workers.auto_pipeline import expire_staged
            expire_staged(root)
            with _inflight_lock:
                room = _MAX_CONCURRENT - len(_inflight)
            left = _publications_left()
            if left <= 0:
                from scout_v2.workers.auto_pipeline import _stage_candidates
                queued = json.loads((root / "queue/candidates.json").read_text(encoding="utf-8"))
                _stage_candidates(root, [k for k, e in queued.items()
                                        if e.get("status") in ("new", "queued", "recheck_failed", "staged")])
                return started
            room = min(room, left)
            if room <= 0:
                return started
            from scout_v2.workers.auto_pipeline import drain_ai_graded
            # Every trigger chooses from the complete FIFO, including old backlog.
            cands = [c["id"] for c in drain_ai_graded(root, limit=room, dry_run=True)]
            for key in cands:
                with _inflight_lock:
                    if len(_inflight) >= _MAX_CONCURRENT:
                        break
                    if key in _inflight:
                        continue
                    ent = _load_candidate(root, key)
                    if ent is None:
                        continue
                    th = _thr.Thread(target=_run_one, args=(ent, rescreen), daemon=True,
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
                    # 每分钟发现并消费候选（用户裁定 2026-11-05）：
                    # 名额未满就当场修，满了 drain 内部自动标 staged 留到
                    # 次日凌晨批量取。这条循环独立于 PR 监控（后者每 15min
                    # 且要扫 30+ 个 PR 的所有端点，会把候选消费挤到几小时一次）。
                    _dispatch(keys=None)
                    _t.sleep(interval)
                except Exception as e:  # noqa: BLE001 — 永不因单次异常停摆
                    try:
                        _audit(root, "auto_repair_error", "-", str(e)[:200])
                    except Exception:  # noqa: BLE001
                        pass
                    _t.sleep(120)

        _thr.Thread(target=_loop, daemon=True).start()

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
                        item = D.refresh_issue(item)
                        queued, added = D.ingest_cloudflare_issues(ds, [item])
                        seen = json.loads((root / "state/seen.json").read_text(encoding="utf-8"))
                        rec = seen.get(key) or {}
                        if rec.get("status") == "retry":
                            raise RuntimeError(rec.get("fine_reason") or "AI unavailable")
                        inbox.finish(key, version)
                        if added:
                            _dispatch()
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
                    import importlib.util
                    spec = importlib.util.spec_from_file_location(
                        "monitor_prs", root.parent / "scripts" / "monitor_prs.py"
                        if (root.parent / "scripts" / "monitor_prs.py").exists()
                        else Path(__file__).resolve().parents[1] / "scripts" / "monitor_prs.py")
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
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

                    from scout_v2.notify import feishu_send
                    for r in results:
                        key = f"{r['repo']}#{r['number']}"
                        action = r.get("action")
                        if action == "fix_ci":
                            _audit(root, "pr_ci_failed", key,
                                   str(r.get("failing_checks"))[:200])
                            # L1/L2：外因已过滤、按 head_sha 去重后派 opencode
                            # 上我们的 fork 分支修。失败本身不阻塞循环。
                            try:
                                out = mod.dispatch_pr_fix(r["repo"], r["number"])
                                _audit(root, "pr_fix_dispatch", key,
                                       f"dispatched={out.get('dispatched')} "
                                       f"reason={str(out.get('reason'))[:120]}")
                                if out.get("dispatched"):
                                    feishu_send(
                                        f"🔧 CI 挂红，已派自动修复：{key}",
                                        f"失败检查：{', '.join(r.get('failing_checks') or [])[:120]}")
                            except Exception as e:  # noqa: BLE001
                                _audit(root, "pr_fix_error", key, str(e)[:200])
                        elif action == "address_review":
                            # 维护者提了具体修改要求 → 先派 opencode 落实，
                            # 再起草回复说明改了什么。草稿仍待人审才发。
                            # 顺序有意义：回复里要写"改了什么"，得先改完。
                            _audit(root, "pr_needs_reply", key,
                                   f"{action} from {r.get('latest_author')}: "
                                   f"{str(r.get('latest_excerpt'))[:150]}")
                            fix_out = {}
                            try:
                                fix_out = mod.dispatch_pr_fix(
                                    r["repo"], r["number"], kind="review",
                                    review_comment=str(r.get("latest_body") or ""),
                                    review_author=str(r.get("latest_author") or ""))
                                _audit(root, "pr_review_fix_dispatch", key,
                                       f"dispatched={fix_out.get('dispatched')} "
                                       f"kind={fix_out.get('kind')} "
                                       f"reason={str(fix_out.get('reason'))[:120]}")
                                if fix_out.get("dispatched"):
                                    # 先标 pending：卡片要能说"正在改"，
                                    # 等 workflow 回调再翻成 pushed
                                    _mark_reply(key, "draft", fix_state="pending",
                                                fix_fork=str(
                                                    fix_out.get("fork_repo") or ""))
                            except Exception as e:  # noqa: BLE001
                                _audit(root, "pr_review_fix_error", key, str(e)[:200])
                        elif action == "answer_question":
                            # 只问问题、没要求改代码 → 只起草回复
                            _audit(root, "pr_needs_reply", key,
                                   f"{action} from {r.get('latest_author')}: "
                                   f"{str(r.get('latest_excerpt'))[:150]}")
                            try:
                                draft = mod.draft_reply(r["repo"], r["number"],
                                                        context=r)
                                _audit(root, "pr_reply_drafted", key,
                                       f"drafted={draft.get('drafted')} "
                                       f"reason={str(draft.get('reason'))[:80]}")
                                if draft.get("drafted"):
                                    _notify_draft(root, r, draft)
                            except Exception as e:  # noqa: BLE001
                                _audit(root, "pr_draft_error", key, str(e)[:200])

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
            while True:
                try:
                    _sync_bitables(root)
                except Exception as ex:
                    _audit(root, "bitable_sync_error", "-", str(ex)[:200])
                time.sleep(900)
        _thr.Thread(target=_loop, daemon=True, name="bitable-sync").start()

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
                    out = D.cloudflare_pull(ds)
                    if "error" not in out:
                        (root / "state" / "cloudflare_last_pull.json").write_text(
                            json.dumps({"at": _t.strftime("%Y-%m-%dT%H:%M:%S"),
                                        "out": out}, ensure_ascii=False),
                            encoding="utf-8")
                except Exception:  # noqa: BLE001
                    pass
                # 默认 300s = 288 次 list/天，占 list 配额不到三成
                _t.sleep(D.cf_pull_seconds(ds))
        _th.Thread(target=_loop, daemon=True).start()

    ds = D.DataSource(root, gh_runner=gh_runner, modal_runner=modal_runner)

    def _reap_orphan_claim():
        from scout_v2.state_store import queue_transaction, ClaimLedger
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
                        "new", "queued", "recheck_failed", "staged")):
                    entry.update(status="escalated", abort_reason="controller restarted; inspect remote run before retry")
                    _audit(root, "orphan_requires_review", key, "daily start retained")

    if os.environ.get("OSSCOUT_DISABLE_AUTO_REPAIR") != "1":
        _reap_orphan_claim()
        _cf_poll_loop()
        _intake_loop()
        _auto_repair_loop()
        _pr_watch_loop()
        _bitable_loop()
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
    # /api/ingest/ 与 /api/pr/reply/fix-result 都用 X-Ingest-Token 自校验
    # （分别是 Worker→看板、pr-fix workflow→看板 的机对机通道），不走会话
    # 认证。fix-result 不加进来的后果：workflow 回调 401，修复状态永远
    # 回不到看板和飞书（2026-11-04 实录）。
    PUBLIC_PATHS = ("/login", "/static", "/favicon.ico", "/api/ingest/",
                    "/api/pr/reply/fix-result", "/feishu/callback")

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
                        primary: str = Form(...),
                        fallback: str = Form("")):
        # fallback 默认 ""：分工独占后空串是常态（FastAPI 对 Form("") 的空表单
        # 值也正常收，这里显式给默认只是兼容老的看板表单不传该字段的情况）。
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

        # AI 起草的回复队列：只有 status=draft 的才展示（sent/dismissed 不再打扰）。
        # 起草是自动的，**发送永远是人审后点击**——对外文字零自动化。
        pending_replies: list[dict] = []
        rp = root / "state" / "pr_replies.json"
        try:
            replies = json.loads(rp.read_text(encoding="utf-8"))
            for key, r in replies.items():
                if r.get("status") != "draft":
                    continue
                repo_part, _, num_part = key.rpartition("#")
                pending_replies.append({**r, "key": key, "repo": repo_part,
                                        "number": num_part})
        except (OSError, json.JSONDecodeError):
            pass

        return render(request, "prs.html", {
            "repo_prs": repo_prs, "any_unknown": any_unknown,
            "external_prs": external, "my_login": my_login,
            "merge_history": ds.merge_history(), "active": "/prs",
            "pending_replies": pending_replies,
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
        return JSONResponse({"dispatched": _dispatch(rescreen=True)})

    @app.get("/api/pr/replies")
    def api_pr_replies():
        """看板拉草稿列表。status: draft=待人审, sent=已发出, dismissed=放弃。"""
        p = root / "state" / "pr_replies.json"
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        return JSONResponse({"replies": data})

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
        if pushed and body.get("kind") == "review" and body.get("review_comment_id"):
            def draft_after_push():
                try:
                    from scripts.monitor_prs import draft_reply, pr_state
                    state = pr_state(repo, num)
                    state.update(pr_title=state.get("title"), pr_url=state.get("url"),
                                 pr_body=state.get("body"), review_comment_id=body["review_comment_id"])
                    state["fix_result"] = extra
                    draft = draft_reply(repo, num, context=state)
                    if draft.get("drafted"):
                        _notify_draft(root, state, draft)
                except Exception as exc:
                    _audit(root, "pr_draft_error", key, str(exc)[:200])
            _thr.Thread(target=draft_after_push, daemon=True).start()
        return JSONResponse({"ok": True})

    @app.post("/api/pr/bitable/sync")
    def api_pr_bitable_sync():
        """手动触发一次多维表格同步（也在 pr_watch 循环里 15 分钟一次）。"""
        import importlib.util
        return JSONResponse(_sync_bitables(root))

    @app.post("/api/pr/reply/send")
    def api_pr_reply_send(request: Request, repo: str = Form(""),
                          number: int = Form(0), body: str = Form("")):
        """发送一条已确认的回复。人是最后一道审——端点只接受看板上改过的文本。"""
        repo = repo.strip()[:120]
        body = body.strip()[:2000]
        if not repo or not number or not body:
            return JSONResponse({"detail": "repo/number/body required"}, status_code=400)
        # 行内/行外由落库的来源决定（草稿生成时记录），表单不传就从 state 读
        try:
            stored = json.loads((root / "state" / "pr_replies.json")
                                .read_text(encoding="utf-8")).get(
                f"{repo}#{number}") or {}
        except (OSError, json.JSONDecodeError):
            stored = {}
        kind = str(stored.get("source_kind") or "issue")
        cid = stored.get("source_comment_id")
        ok, detail = _post_pr_comment(repo, number, body,
                                      kind=kind, comment_id=cid)
        if not ok:
            _audit(root, "pr_reply_send_failed", f"{repo}#{number}", detail)
            return JSONResponse({"detail": detail or "gh failed"}, status_code=502)
        _mark_reply(f"{repo}#{number}", "sent", sent_body=body,
                    sent_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
                    sent_kind=kind if kind == "inline" else "issue",
                    sent_comment_id=str(cid) if cid else None)
        _audit(root, "pr_reply_sent", f"{repo}#{number}",
               f"kind={kind} " + body[:160])
        return JSONResponse({"ok": True})

    # ---------------- 飞书卡片回调（卡片里一键发送） ----------------

    @app.post("/feishu/callback")
    async def feishu_callback(request: Request):
        """飞书卡片动作回调：回复草稿卡片里的「一键发送」走这里。

        URL 验证（首次配置回调时飞书发的 challenge）在这里原样回显；
        表单提交（form_submit）解析编辑后的 reply_text，以本账号发到 GitHub，
        并把草稿标记为 sent。verification token 用于校验请求确实来自飞书。
        """
        import sys as _sys
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

    @app.post("/api/pr/reply/dismiss")
    def api_pr_reply_dismiss(request: Request, repo: str = Form(...),
                             number: int = Form(...)):
        """放弃草稿（人工决定不回或自己回了）。"""
        _mark_reply_key = f"{repo}#{number}"
        rp = root / "state" / "pr_replies.json"
        try:
            replies = json.loads(rp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            replies = {}
        key = f"{repo}#{number}"
        if key in replies:
            replies[key]["status"] = "dismissed"
            tmp = rp.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(replies, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            os.replace(tmp, rp)
        _audit(root, "pr_reply_dismissed", key, "")
        return JSONResponse({"ok": True})

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

        fresh = 0
        for ev in raw[:200]:
            if not isinstance(ev, dict):
                continue
            cid = str(ev.get("comment_id") or "")
            repo = str(ev.get("repo", ""))[:120]
            num = int(ev.get("number") or 0)
            if not cid or not repo or not num or cid in seen or not modifiable_repo(repo):
                continue
            seen[cid] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                         "repo": repo, "number": num}
            # 复用 PR 监控的分类逻辑，保证两条通道判定一致
            try:
                import importlib.util
                spec = importlib.util.spec_from_file_location(
                    "monitor_prs",
                    Path(__file__).resolve().parents[1] / "scripts" / "monitor_prs.py")
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                cls = mod.classify({
                    "comments": [{"author": {"login": ev.get("author", "")},
                                  "body": ev.get("body", ""),
                                  "createdAt": cid}],
                    "statusCheckRollup": [],
                    "repo": repo, "number": num, "title": "",
                })
                action = cls.get("action", "wait")
            except Exception:  # noqa: BLE001
                action = "unknown"
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
            if action in ("address_review", "answer_question"):
                ev_copy = dict(ev)
                ev_copy["action"] = action
                _thr.Thread(target=_handle_pr_event,
                            args=(root, ev_copy), daemon=True).start()

        if fresh:
            tmp = seen_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(seen, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, seen_path)
            _audit(root, "pr_comment", f"{fresh} new",
                   ", ".join(f"{r['repo']}#{r['number']}" for r in
                             list(seen.values())[-3:]))
        return JSONResponse({"received": len(raw), "new": fresh})

    @app.post("/api/dashboard/settings")
    def api_dashboard_settings(request: Request,
                               cf_pull_seconds: int = Form(300),
                               pr_refresh_seconds: int = Form(120),
                               gh_cache_ttl: int = Form(300)):
        cfg = {**D.dashboard_settings(ds), "cf_pull_seconds": max(300, min(3600, cf_pull_seconds)),
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
