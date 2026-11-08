"""Editable model settings in the existing Feishu Base. Never export credentials."""

from hashlib import sha256
from pathlib import Path

from . import feishu_app, feishu_bitable as fb
from .console_models import model_setting_error, public_models, read_models, write_models


TABLE = "模型配置"
SETTINGS = {
    "OpenCode 修复模型": "repair_model",
    "OpenCode 回复模型": "reply_model",
    "Codiv Base URL": "codiv_base",
    "Codiv 细筛模型": "codiv_model",
}
CREDENTIALS = {"OpenCode API 密钥": "opencode_configured", "Codiv API 密钥": "codiv_configured"}
FIELDS = [{"field_name": name, "type": 1} for name in ("键", "当前生效", "修改为", "应用状态")] + [
    {"field_name": "安全设置", "type": 15},
]


def sync_model_settings(root: Path, model_lock, settings_url: str) -> str:
    if not fb.configured(root):
        return ""
    app_token = fb._binding_app_token(root)
    binding = fb.load_binding(root)
    table_id = binding.get("model_table_id") if binding.get("app_token") == app_token else None
    if not table_id:
        table_id = fb.find_or_create_table(app_token, TABLE, FIELDS)
        fb.save_binding(root, {"app_token": app_token, "model_table_id": table_id})
    records = fb.load_records(app_token, table_id)

    with model_lock:
        prior = read_models(root)
        values = dict(prior)
        # Remember the last applied Feishu edit so an unchanged cell cannot
        # overwrite a newer edit made in the authenticated console.
        applied = dict(prior.get("_feishu_model_edits") or {})
        statuses = {}
        for label, key in SETTINGS.items():
            entered = (records.get(label, {}).get("fields") or {}).get("修改为")
            value = entered.strip() if isinstance(entered, str) else ""
            if not value:
                applied.pop(key, None)
                statuses[label] = "填写「修改为」以更新"
                continue
            if len(value) > 512 or value.lower().startswith(("sk-", "oc_")):
                statuses[label] = "格式有误，请填写模型或地址"
                continue
            error = model_setting_error(key, value)
            if error:
                statuses[label] = error
                continue
            if key == "codiv_base":
                value = value.rstrip("/")
            fingerprint = sha256(value.encode()).hexdigest()
            if applied.get(key) != fingerprint:
                values[key] = value
                applied[key] = fingerprint
            statuses[label] = "已应用" if values.get(key) == value else "已从安全设置更新"
        if applied or "_feishu_model_edits" in prior:
            values["_feishu_model_edits"] = applied
        if values != prior:
            write_models(root, values)
        effective = public_models(root)

    fields = {label: {"当前生效": effective[key], "应用状态": statuses[label]}
              for label, key in SETTINGS.items()}
    fields.update({label: {
        "当前生效": "已配置" if effective[key] else "未配置",
        "应用状态": "请在安全设置页更新密钥",
        "安全设置": {"text": "打开安全设置", "link": settings_url},
    } for label, key in CREDENTIALS.items()})
    creates, updates = [], []
    for label, desired in fields.items():
        existing = records.get(label)
        if not existing:
            creates.append({"fields": {"键": label, **desired}})
        elif any(existing["fields"].get(name) != value for name, value in desired.items()):
            updates.append({"record_id": existing["record_id"], "fields": desired})
    # Update only the columns owned by the service, preserving any concurrent cell edit.
    for action, batch in (("batch_create", creates), ("batch_update", updates)):
        if batch:
            response = feishu_app._request(
                "POST", f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}"
                        f"/tables/{table_id}/records/{action}",
                {"records": batch}, token=feishu_app.tenant_token())
            if response.get("code") != 0:
                raise RuntimeError(f"Feishu model settings {action}: {response.get('code')}")
    return f"https://my.feishu.cn/base/{app_token}?table={table_id}"
