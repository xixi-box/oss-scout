"""飞书多维表格的链接台账与批量差量同步。"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from urllib.parse import urlencode

from . import feishu_app

TABLE_NAME = "PR 记录"
REPO_TABLE_NAME = "仓库池"
STATE_FILE = "feishu_bitable.json"
_BINDING_LOCK = threading.RLock()

CAND_TABLE_NAME = "候选池"
SCAN_TABLE_NAME = "扫描发现"
LINK_FIELDS = [{"field_name": "键", "type": 1}]


def _state_path(root: Path) -> Path:
    data_root = os.environ.get("OSSCOUT_DASHBOARD_ROOT", "")
    base = Path(data_root) if data_root else root
    return base / "state" / STATE_FILE


def load_binding(root: Path) -> dict:
    try:
        return json.loads(_state_path(root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_binding(root: Path, binding: dict) -> None:
    from .state_store import atomic_json_write
    with _BINDING_LOCK:
        previous = load_binding(root)
        # Table IDs belong to a base. A new base must not inherit stale IDs.
        if (previous.get("app_token") and binding.get("app_token")
                and previous["app_token"] != binding["app_token"]):
            previous = {}
        atomic_json_write(_state_path(root), {**previous, **binding})


def _binding_app_token(root: Path) -> str:
    """表格绑定：环境变量优先，其次 state 文件（首次发现后缓存）。"""
    env_tok = (os.environ.get("FEISHU_BITABLE_APP_TOKEN") or "").strip()
    if env_tok:
        return env_tok
    return str(load_binding(root).get("app_token") or "")


def configured(root: Path) -> bool:
    """有应用凭证且表格已绑定（env 或 state），才算就绪。"""
    return feishu_app.app_configured() and bool(_binding_app_token(root))


def pr_table_url(root: Path) -> str:
    """Resolve the existing PR table without discovering or creating a new one."""
    import re
    binding = load_binding(root)
    app, table = _binding_app_token(root), str(binding.get("pr_table_id") or "")
    if app == "IiMGbnljvaosNdsBOyUcQFkEnhd" and not table:
        return f"https://my.feishu.cn/base/{app}?table=tblQktvgCCvceN9a"
    if (app == binding.get("app_token") and re.fullmatch(r"[A-Za-z0-9]+", app)
            and re.fullmatch(r"tbl[A-Za-z0-9]+", table)):
        return f"https://my.feishu.cn/base/{app}?table={table}"
    return ""


def link_key(label: str, kind: str) -> list[dict]:
    pattern = r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"
    match = re.fullmatch(pattern if kind == "repo" else pattern + r"#([0-9]+)", label)
    if not match:
        raise ValueError(f"Invalid GitHub key: {label!r}")
    path = match[1] if kind == "repo" else f"{match[1]}/{kind}/{match[2]}"
    return [{"type": "url", "text": label, "link": f"https://github.com/{path}"}]


def row_to_fields(row: dict) -> dict:
    return {"键": link_key(f"{row['repo']}#{row['number']}", "pull")}


def repo_row_to_fields(row: dict) -> dict:
    return {"键": link_key(row["repo"], "repo")}


def issue_row_to_fields(row: dict) -> dict:
    return {"键": link_key(row["key"], "issues")}


def ensure_columns(app_token: str, table_id: str, fields: list) -> None:
    """给已存在的表补缺失的字段（如扫描发现后加的 入队状态 列）。"""
    tok = feishu_app.tenant_token()
    body = feishu_app._request(
        "GET", f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}"
               f"/tables/{table_id}/fields?page_size=100", token=tok)
    if body.get("code") not in (0, None):
        return
    existing = {f.get("field_name") for f in (body.get("data") or {}).get("items") or []}
    for spec in fields:
        if spec.get("field_name") not in existing:
            feishu_app._request(
                "POST", f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}"
                        f"/tables/{table_id}/fields", spec, token=tok)


def find_or_create_table(app_token: str, table_name: str = TABLE_NAME,
                         fields: list | None = None) -> str:
    """按名字找表，没有就按字段规格建；已存在的表补缺失字段。"""
    tok = feishu_app.tenant_token()
    body = feishu_app._request(
        "GET", f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables"
               f"?page_size=100", token=tok)
    if body.get("code") not in (0, None):
        raise RuntimeError(f"list tables: {str(body)[:200]}")
    for t in (body.get("data") or {}).get("items") or []:
        if t.get("name") == table_name:
            tid = str(t["table_id"])
            ensure_columns(app_token, tid, fields or LINK_FIELDS)
            return tid
    created = feishu_app._request(
        "POST", f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables",
        {"table": {"name": table_name, "fields": fields or LINK_FIELDS}}, token=tok)
    if created.get("code") != 0:
        raise RuntimeError(f"create table: {str(created)[:300]}")
    return str((created.get("data") or {}).get("table_id"))


def _norm_field(v):
    """归一化飞书返回的字段值，使指纹比较稳定。

    飞书读回来的文本是 [{"type":"text","text":...}] 段落形态、链接会带
    额外键——和写入时的形态不同。不归一化的话，每轮同步都会把所有行
    "看起来变了"，白白重写整张表。
    """
    if isinstance(v, list):
        parts = [str(seg.get("text") or "") for seg in v
                 if isinstance(seg, dict) and seg.get("text") is not None]
        return "".join(parts) if parts else v
    if isinstance(v, dict) and "text" in v and "link" in v:
        return {"text": v.get("text") or "", "link": v.get("link") or ""}
    return v


def _key_label(value) -> str:
    if isinstance(value, list):
        return "".join(str(seg.get("text") or "") for seg in value if isinstance(seg, dict))
    return str(value or "")


def record_pages(app_token: str, table_id: str):
    """Yield raw records, including rich text links and blank primary cells."""
    tok = feishu_app.tenant_token()
    params = {"page_size": 500, "text_field_as_array": "true"}
    while True:
        url = (f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}"
               f"/tables/{table_id}/records?{urlencode(params)}")
        body = feishu_app._request("GET", url, token=tok)
        if body.get("code") != 0:
            raise RuntimeError(f"list records: {str(body)[:200]}")
        data = body.get("data") or {}
        yield from data.get("items") or []
        if not data.get("has_more"):
            break
        page_token = data.get("page_token")
        if not page_token or page_token == params.get("page_token"):
            raise RuntimeError("Invalid Feishu records page cursor")
        params["page_token"] = page_token


def load_records(app_token: str, table_id: str) -> dict:
    out = {}
    for record in record_pages(app_token, table_id):
        raw = record.get("fields") or {}
        key = _key_label(raw.get("键"))
        if not key:
            continue
        fields = {name: _norm_field(value) for name, value in raw.items()}
        if isinstance(raw.get("键"), list):
            segments = raw["键"]
            if len(segments) == 1 and segments[0].get("type") == "url" and segments[0].get("link"):
                fields["键"] = [{name: segments[0][name] for name in ("type", "text", "link")}]
        out[key] = {"record_id": str(record["record_id"]), "fields": fields}
    return out


def _fingerprint(fields: dict) -> str:
    return json.dumps(fields, ensure_ascii=False, sort_keys=True, default=str)


def sync_rows(root: Path, rows: list[dict], *, force: bool = False) -> dict:
    """把 PR 行同步进「PR 记录」表。见 sync_table()。"""
    return sync_table(root, rows, table_name=TABLE_NAME, fields=LINK_FIELDS,
                      to_fields=row_to_fields, binding_key="pr_table_id",
                      force=force)


def sync_table(root: Path, rows: list[dict], *, table_name: str, fields: list,
               to_fields, binding_key: str, force: bool = False,
               allow_empty: bool = False) -> dict:
    """把行 upsert 进指定表；表和字段不存在就建。返回计数摘要。

    同一个 base 里可挂多张表（PR 记录 / 仓库池 / 候选池…），每张表用自己的
    binding_key 缓存 table_id。键列恒为"键"（repo#number 或 repo 本身）。
    allow_empty 仅在确认拿到完整快照时启用，空结果也会清理旧行。
    默认保留空结果保护，避免源查询故障清空飞书。
    """
    binding = load_binding(root)
    app_token = _binding_app_token(root)
    if not app_token:
        raise RuntimeError(
            "no bitable app_token — 设 FEISHU_BITABLE_APP_TOKEN，或把表格链接"
            "里的 token 存进 state/feishu_bitable.json")
    cached_table = binding.get(binding_key) if binding.get("app_token") == app_token else None
    table_id = cached_table or find_or_create_table(
        app_token, table_name, fields)
    save_binding(root, {"app_token": app_token, binding_key: table_id})
    ensure_columns(app_token, table_id, fields or [])
    # 兼容其他使用 sync_table 的表，自动补齐转换器产出的字段。
    produced = set()
    for row in rows[:5]:
        produced.update(to_fields(row).keys())
    declared = {f.get("field_name") for f in (fields or [])}
    missing = sorted(produced - declared)
    if missing:
        ensure_columns(app_token, table_id,
                       [{"field_name": n, "type": 1} for n in missing])

    tok = feishu_app.tenant_token()
    existing = load_records(app_token, table_id)
    all_fields = [to_fields(r) for r in rows]
    to_create, to_update = [], []
    for fields in all_fields:
        key = _key_label(fields["键"])
        hit = existing.get(key)
        if hit is None:
            to_create.append({"fields": fields})
        elif force or _fingerprint({name: hit["fields"].get(name) for name in fields}) != _fingerprint(fields):
            to_update.append({"record_id": hit["record_id"], "fields": fields})

    def _post(path, payload, method="POST"):
        body = feishu_app._request(
            method, f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}"
                    f"/tables/{table_id}/{path}", payload, token=tok)
        if body.get("code") not in (0, None):
            raise RuntimeError(f"{path}: {str(body)[:300]}")
        return body

    # 分批：单批 ≤500 条
    for i in range(0, len(to_create), 500):
        _post("records/batch_create", {"records": to_create[i:i + 500]})
    for i in range(0, len(to_update), 500):
        _post("records/batch_update", {"records": to_update[i:i + 500]})

    # 剔除：口径外的记录（closed 未合并 / 自有仓库 / optica）从表里删掉。
    # 只在拿到非空行集时执行——GitHub 全挂时 rows 为空，那不能当"全部剔除"。
    pruned = 0
    if rows or allow_empty:
        incoming = {_key_label(f["键"]) for f in all_fields}
        stale = [hit["record_id"] for key, hit in existing.items()
                 if key not in incoming]
        for i in range(0, len(stale), 500):
            _post("records/batch_delete", {"records": stale[i:i + 500]})
        pruned = len(stale)

    return {"created": len(to_create), "updated": len(to_update),
            "skipped": len(rows) - len(to_create) - len(to_update),
            "pruned": pruned}
