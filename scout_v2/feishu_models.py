"""Bidirectional model settings in the existing Feishu Base."""

from hashlib import sha256
from pathlib import Path

from . import feishu_app, feishu_bitable as fb
from .console_models import effective_models, model_setting_error, read_models, rotate_opencode_key, write_models

TABLE = "配置"
PROVIDERS = {
    "运行设置": {"修复工具": "repair_agent",
                 "候选池大小": "candidate_pool_size", "每日 PR 额度": "daily_pr_quota"},
    "OpenCode": {"模型名": "repair_model", "API 密钥": "opencode_key",
                 "Base URL": "opencode_base", "思考强度": "opencode_variant"},
    "Codiv": {"模型名": "codiv_model", "API 密钥": "codiv_key", "Base URL": "codiv_base",
              "思考预算 (tokens)": "codiv_think"},
    "Codex": {"模型名": "codex_model", "思考强度": "codex_variant"},
}
FIELDS = [{"field_name": name, "type": 1} for name in (
    "键", "模型名", "API 密钥", "Base URL", "思考强度", "思考预算 (tokens)", "应用状态")]
FIELDS.append({"field_name": "修复工具", "type": 3, "property": {
    "options": [{"name": "OpenCode", "color": 0}, {"name": "Codex", "color": 4}]}})
FIELDS.extend({"field_name": name, "type": 2} for name in ("候选池大小", "每日 PR 额度"))
_LIMIT_KEYS = {"candidate_pool_size", "daily_pr_quota"}
_LEGACY = {"repair_model": "OpenCode 修复模型", "reply_model": "OpenCode 回复模型",
           "codiv_base": "Codiv Base URL", "codiv_model": "Codiv 细筛模型"}
_OLD_COLUMNS = {"当前生效", "修改为", "安全设置", "PR 评论触发", "回复模型"}
_BASE_URL_KEYS = {"codiv_base", "opencode_base"}


def _field_value(value, key):
    if key in _LIMIT_KEYS and isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(int(value)) if isinstance(value, int) or value.is_integer() else str(value)
    if isinstance(value, list) and len(value) == 1 and key in _BASE_URL_KEYS:
        value = value[0]
    if isinstance(value, dict) and key in _BASE_URL_KEYS:
        value = value.get("link")
    value = value.strip() if isinstance(value, str) else ""
    if key == "repair_agent":
        return value.lower()
    return "" if key in {"opencode_variant", "codex_variant", "codex_model"} and value in {"默认", "default"} else value


def _digest(value):
    return sha256(value.encode()).hexdigest()


def _table_request(table_url, method, path, payload=None):
    response = feishu_app._request(method, f"{table_url}/{path}", payload, token=feishu_app.tenant_token())
    if response.get("code") != 0:
        raise RuntimeError(f"Feishu model settings {method} {path}: {response.get('code')}")
    return response.get("data") or {}


def sync_model_settings(root: Path, model_lock) -> str:
    if not fb.configured(root):
        return ""
    app_token = fb._binding_app_token(root)
    binding = fb.load_binding(root)
    same_base = binding.get("app_token") == app_token
    table_id = binding.get("model_table_id") if same_base else None
    migrating = not (same_base and table_id and binding.get("model_table_layout") == 6)
    if not table_id:
        table_id = fb.find_or_create_table(app_token, TABLE, FIELDS)
        fb.save_binding(root, {"app_token": app_token, "model_table_id": table_id})
    elif migrating:
        fb.ensure_columns(app_token, table_id, FIELDS)
    table_url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}"
    if binding.get("model_table_name") != TABLE:
        response = feishu_app._request("PATCH", table_url, {"name": TABLE},
                                       token=feishu_app.tenant_token())
        if response.get("code") != 0:
            raise RuntimeError(f"Feishu settings table rename: {response.get('code')}")
        fb.save_binding(root, {"model_table_name": TABLE})
    records = fb.load_records(app_token, table_id)

    with model_lock:
        prior = read_models(root)
        values = dict(prior)
        effective = effective_models(root)
        last = dict(prior.get("_feishu_row_values") or {})
        next_last = dict(last)
        creates, updates = [], []
        for label, columns in PROVIDERS.items():
            existing = records.get(label)
            remote = existing["fields"] if existing else {}
            desired, errors = {}, []
            for column, key in columns.items():
                current = effective[key]
                if existing:
                    entered = _field_value(remote.get(column), key)
                    if key == "opencode_base" and not entered and key not in last:
                        entered = current
                    if key in _LIMIT_KEYS and not entered and key not in last:
                        entered = current
                else:
                    old = records.get(_LEGACY.get(key), {}).get("fields", {})
                    entered = _field_value(old.get("修改为"), key)
                    if not entered or _digest(entered.rstrip("/") if key in _BASE_URL_KEYS else entered) == (
                            prior.get("_feishu_model_edits") or {}).get(key):
                        entered = current
                if entered != current and (not existing or _digest(entered) != last.get(key)):
                    if key in _BASE_URL_KEYS:
                        entered = entered.rstrip("/")
                    elif key == "codiv_think" and not entered:
                        entered = "0"
                    error = model_setting_error(key, entered)
                    if not error and key == "repair_agent" and entered == "codex":
                        from .codex_auth import auth_ready
                        if not auth_ready(root):
                            error = "Codex 待登录 ChatGPT"
                    if not error and key == "codiv_think" and int(entered) and not str(
                            values.get("codiv_model") or effective["codiv_model"]).startswith(("openjev-", "jev-")):
                        error = "思考预算适用于 OpenJev 模型。"
                    if not error and key == "codiv_model" and not entered.startswith(("openjev-", "jev-")) and int(
                            values.get("codiv_think", effective["codiv_think"])):
                        error = "切换到其他模型前，请将思考预算设为 0。"
                    if not error and entered != current and key == "opencode_key":
                        try:
                            rotate_opencode_key(root, entered)
                        except (RuntimeError, ValueError) as exc:
                            error = str(exc)
                    if error:
                        errors.append(f"{column}：{error}")
                        if existing:
                            continue
                    else:
                        current = str(int(entered)) if key == "codiv_think" else entered
                        if current != effective[key]:
                            values[key] = current
                if key == "repair_agent":
                    desired[column] = {"opencode": "OpenCode", "codex": "Codex"}[current]
                elif key in _LIMIT_KEYS:
                    desired[column] = int(current)
                else:
                    desired[column] = current or (
                        "默认" if key in {"opencode_variant", "codex_variant", "codex_model"} else None)
                next_last[key] = _digest(current)
            desired["应用状态"] = "；".join(errors) if errors else "已同步"
            if label == "Codex":
                from .codex_auth import auth_ready
                desired["应用状态"] = ("；".join(errors) if errors else "已同步") + (
                    " · ChatGPT 已登录" if auth_ready(root) else " · 待登录 ChatGPT")
            if existing:
                changed = {key: value for key, value in desired.items() if remote.get(key) != value}
                if changed:
                    updates.append({"record_id": existing["record_id"], "fields": changed})
            else:
                creates.append({"fields": {"键": label, **desired}})

        values.pop("pr_comment_trigger", None)
        next_last.pop("pr_comment_trigger", None)
        next_last.pop("reply_model", None)
        if values != prior:
            write_models(root, values)
        applied = effective_models(root)
        if migrating or any(effective[key] != applied[key] for key in _LIMIT_KEYS):
            _apply_runtime_limits(root, effective, applied)
        for action, batch in (("batch_create", creates), ("batch_update", updates)):
            if batch:
                _table_request(table_url, "POST", f"records/{action}", {"records": batch})
        if next_last != last:
            write_models(root, {**values, "_feishu_row_values": next_last})

    if migrating:
        old_names = {*_LEGACY.values(), "OpenCode API 密钥", "Codiv API 密钥"}
        stale = [records[name]["record_id"] for name in old_names if name in records]
        if stale:
            _table_request(table_url, "POST", "records/batch_delete", {"records": stale})
        fields = _table_request(table_url, "GET", "fields?page_size=100")
        for field in fields.get("items", []):
            if field["field_name"] in _OLD_COLUMNS:
                _table_request(table_url, "DELETE", f"fields/{field['field_id']}")
        fb.save_binding(root, {"app_token": app_token, "model_table_id": table_id, "model_table_layout": 6})
    return f"https://my.feishu.cn/base/{app_token}?table={table_id}"


def _apply_runtime_limits(root: Path, before: dict, after: dict) -> None:
    from .state_store import business_now, queue_transaction, rotate_staged_queue
    from .workers.auto_pipeline import _discovered_today, log

    if not (root / "queue/candidates.json").exists():
        return
    now = business_now().isoformat()
    with queue_transaction(root) as queue:
        if int(after["daily_pr_quota"]) > int(before["daily_pr_quota"]):
            for entry in queue.values():
                if (entry.get("status") == "staged" and entry.get("defer_reason") == "daily_quota"
                        and _discovered_today(entry) and not entry.get("dispatch_started_at")
                        and not entry.get("direct_start_reserved_at")):
                    entry.update(defer_reason="execution_wait", eligible_at=now, updated_at=now)
        removed = rotate_staged_queue(root, queue)
    for key in removed:
        log(root, "queue_evicted", key, "configured rolling capacity")
