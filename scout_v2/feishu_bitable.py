"""飞书多维表格：我们所有 PR 的记录同步。

分工：
  monitor_prs.build_pr_rows()  从 GitHub + 本地状态拼出"每条 PR 一行"的数据
  本模块                        只管飞书侧：找表格、建字段、增改记录

表格结构（键列是 repo#number，重跑不重复）：
  键 | 仓库 | PR号 | 标题 | 状态 | 最近动作 | 回复状态 | 修改状态 |
  创建时间 | 最近活动 | 关闭时间 | 链接 | 备注

设计约束：
  - 记录用"键"做 upsert：改字段走 batch_update，新的走 batch_create，
    不删除——PR 关闭了也应该留在表里（这正是这张表的意义）。
  - 配置持久化在 state/feishu_bitable.json（app_token/table_id），
    找到一次就记住，之后每轮同步零发现成本。
  - 任何异常都向上抛给调用方记审计，绝不静默——数据没同步出去要说出来。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import feishu_app

TABLE_NAME = "PR 记录"
REPO_TABLE_NAME = "仓库池"
STATE_FILE = "feishu_bitable.json"

# 「仓库池」表：监控名单 + 池审计（语言/stars/最近 push）。评分列等
# pool_rates 把指标落盘后再补。

# 「候选池」表：queue/candidates.json 的镜像——真正进入流水线的 issue。
# 处理状态用颜色语义：黄=排队中（含等确认），绿=已解决（PR 已开出），
# 红=已失效（stale/竞争失败/尝试超限等）。
CAND_TABLE_NAME = "候选池"
CAND_FIELDS = [
    {"field_name": "键", "type": 1},
    {"field_name": "仓库", "type": 1},
    {"field_name": "issue号", "type": 2},
    {"field_name": "标题", "type": 1},
    {"field_name": "车道", "type": 1},
    {"field_name": "处理状态", "type": 3, "property": {"options": [
        {"name": "排队中", "color": 2},      # 黄
        {"name": "已解决", "color": 3},      # 绿
        {"name": "已失效", "color": 0},      # 红
        {"name": "等确认", "color": 0},
    ]}},
    {"field_name": "AI判定", "type": 1},
    {"field_name": "入队时间", "type": 5},
    {"field_name": "更新时间", "type": 5},
    {"field_name": "链接", "type": 15},        # issue 链接
    {"field_name": "PR链接", "type": 15},      # 开出的 PR
    {"field_name": "备注", "type": 1},
]

# 「扫描发现」表：seen.json 的镜像——Worker 扫到的每个 issue 和它的去向
# （入队 / 被规则否决 / 低分 / 未知仓库）。回答"这个 issue 为什么没被处理"。
SCAN_TABLE_NAME = "扫描发现"
SCAN_FIELDS = [
    {"field_name": "键", "type": 1},
    {"field_name": "仓库", "type": 1},
    {"field_name": "issue号", "type": 2},
    {"field_name": "状态", "type": 1},
    # 粗筛=规则层否决（提问/文档/计费/标签/范围外）；细筛=AI 判定理由
    {"field_name": "粗筛原因", "type": 1},
    {"field_name": "细筛原因", "type": 1},
    {"field_name": "入队状态", "type": 3, "property": {"options": [
        {"name": "排队中", "color": 2},
        {"name": "已解决", "color": 3},
        {"name": "已失效", "color": 0},
    ]}},
    {"field_name": "链接", "type": 15},
    {"field_name": "发现时间", "type": 5},
]
REPO_FIELDS = [
    {"field_name": "键", "type": 1},
    {"field_name": "仓库", "type": 1},
    {"field_name": "owner", "type": 1},
    {"field_name": "车道", "type": 3, "property": {"options": [
        {"name": "open"}, {"name": "gated"}, {"name": "restricted"}]}},
    {"field_name": "语言", "type": 1},
    {"field_name": "stars", "type": 2},
    {"field_name": "最近push", "type": 1},
    {"field_name": "内部", "type": 3, "property": {"options": [
        {"name": "是"}]}},
    {"field_name": "备注", "type": 1},
]

# 字段类型：1 文本 2 数字 3 单选 5 日期时间 15 超链接
FIELDS = [
    {"field_name": "键", "type": 1},
    {"field_name": "仓库", "type": 1},
    {"field_name": "PR号", "type": 2},
    {"field_name": "标题", "type": 1},
    {"field_name": "状态", "type": 3, "property": {"options": [
        {"name": "open"}, {"name": "merged"}, {"name": "closed"}]}},
    {"field_name": "最近动作", "type": 3, "property": {"options": [
        {"name": "fix_ci"}, {"name": "address_review"},
        {"name": "answer_question"}, {"name": "assignment_note"},
        {"name": "wait"}, {"name": "unknown"}]}},
    {"field_name": "回复状态", "type": 3, "property": {"options": [
        {"name": "待审"}, {"name": "已发送"}, {"name": "已放弃"}]}},
    {"field_name": "修改状态", "type": 3, "property": {"options": [
        {"name": "进行中"}, {"name": "已推送"}, {"name": "失败"}]}},
    {"field_name": "创建时间", "type": 5},
    {"field_name": "最近活动", "type": 5},
    {"field_name": "关闭时间", "type": 5},
    {"field_name": "链接", "type": 15},
    {"field_name": "备注", "type": 1},
]


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
    p = _state_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(binding, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def _binding_app_token(root: Path) -> str:
    """表格绑定：环境变量优先，其次 state 文件（首次发现后缓存）。"""
    env_tok = (os.environ.get("FEISHU_BITABLE_APP_TOKEN") or "").strip()
    if env_tok:
        return env_tok
    return str(load_binding(root).get("app_token") or "")


def configured(root: Path) -> bool:
    """有应用凭证且表格已绑定（env 或 state），才算就绪。"""
    return feishu_app.app_configured() and bool(_binding_app_token(root))


def _iso_to_ms(v) -> int | None:
    if not v:
        return None
    try:
        # GitHub 的时间是 UTC；mktime 按本地时区解释会偏移几个小时，
        # Windows 上还会直接 OverflowError。timegm 才是 UTC 语义。
        import calendar
        return int(calendar.timegm(
            time.strptime(str(v)[:19], "%Y-%m-%dT%H:%M:%S")) * 1000)
    except ValueError:
        return None


def row_to_fields(row: dict) -> dict:
    """把 build_pr_rows() 的一行转成 Bitable 字段值。"""
    f = {
        "键": f"{row['repo']}#{row['number']}",
        "仓库": row["repo"],
        "PR号": int(row["number"]),
        "标题": str(row.get("title") or "")[:800],
        "链接": {"text": f"{row['repo']}#{row['number']}",
                 "link": row.get("url") or ""},
    }
    for col, key in (("状态", "state"), ("最近动作", "action"),
                     ("回复状态", "reply_status"), ("修改状态", "fix_state")):
        v = str(row.get(key) or "").strip()
        if v:
            f[col] = v
    for col, key in (("创建时间", "created_at"), ("最近活动", "updated_at"),
                     ("关闭时间", "closed_at")):
        ms = _iso_to_ms(row.get(key))
        if ms:
            f[col] = ms
    note = str(row.get("note") or "").strip()
    if note:
        f["备注"] = note[:800]
    return f


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
            ensure_columns(app_token, tid, fields or FIELDS)
            return tid
    created = feishu_app._request(
        "POST", f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables",
        {"table": {"name": table_name, "fields": fields or FIELDS}}, token=tok)
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


def load_records(app_token: str, table_id: str) -> dict:
    """现有记录：{键: {"record_id":..., "fields": 归一化后的字段}}。分页拉全。"""
    tok = feishu_app.tenant_token()
    out: dict = {}
    page_token = ""
    while True:
        url = (f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}"
               f"/tables/{table_id}/records?page_size=500")
        if page_token:
            url += f"&page_token={page_token}"
        body = feishu_app._request("GET", url, token=tok)
        if body.get("code") not in (0, None):
            raise RuntimeError(f"list records: {str(body)[:200]}")
        data = body.get("data") or {}
        # 数字列飞书回传字符串、文本列回传段落——不归一化的话每轮同步的
        # 指纹永远对不上，整表每 15 分钟被无意义重写一遍（扫描表 1300+ 行）
        NUMBER_FIELDS = ("PR号", "issue号", "stars")
        for it in data.get("items") or []:
            fields = {k: _norm_field(v) for k, v in (it.get("fields") or {}).items()}
            for col in NUMBER_FIELDS:
                if isinstance(fields.get(col), str) and fields[col].isdigit():
                    fields[col] = int(fields[col])
            key = str(fields.get("键") or "")
            if key:
                out[key] = {"record_id": str(it.get("record_id")),
                            "fields": fields}
        if data.get("has_more") and data.get("page_token"):
            page_token = data["page_token"]
        else:
            break
    return out


def _fingerprint(fields: dict) -> str:
    return json.dumps(fields, ensure_ascii=False, sort_keys=True, default=str)


def repo_row_to_fields(row: dict) -> dict:
    f = {
        "键": row["repo"],
        "仓库": row["repo"],
        "owner": str(row["repo"]).split("/", 1)[0],
    }
    if row.get("lane"):
        f["车道"] = str(row["lane"])
    if row.get("lang"):
        f["语言"] = str(row["lang"])
    if isinstance(row.get("stars"), int):
        f["stars"] = row["stars"]
    if row.get("pushed"):
        f["最近push"] = str(row["pushed"])[:10]
    if row.get("internal"):
        f["内部"] = "是"
    if row.get("note"):
        f["备注"] = str(row["note"])[:800]
    return f


def scan_row_to_fields(row: dict) -> dict:
    repo, _, num = str(row.get("key") or "").partition("#")
    f = {
        "键": row.get("key") or "",
        "仓库": repo,
    }
    if num.isdigit():
        f["issue号"] = int(num)
        f["链接"] = {"text": f"{repo}#{num}",
                     "link": f"https://github.com/{repo}/issues/{num}"}
    if row.get("status"):
        f["状态"] = str(row["status"])
    if row.get("coarse_reason"):
        f["粗筛原因"] = str(row["coarse_reason"])[:500]
    if row.get("fine_reason"):
        f["细筛原因"] = str(row["fine_reason"])[:500]
    if row.get("queue_state"):
        f["入队状态"] = str(row["queue_state"])
    if row.get("first_seen"):
        ms = _iso_to_ms(str(row["first_seen"]).replace(" ", "T"))
        if ms:
            f["发现时间"] = ms
    return f


def cand_row_to_fields(row: dict) -> dict:
    f = {
        "键": row.get("key") or "",
        "仓库": str(row.get("key") or "").partition("#")[0],
    }
    num = str(row.get("key") or "").partition("#")[2]
    if num.isdigit():
        f["issue号"] = int(num)
        repo = f["仓库"]
        f.setdefault("链接", {"text": f"{repo}#{num}",
                              "link": f"https://github.com/{repo}/issues/{num}"})
    if row.get("title"):
        f["标题"] = str(row["title"])[:500]
    if row.get("lane"):
        f["车道"] = str(row["lane"])
    if row.get("queue_state"):
        f["处理状态"] = str(row["queue_state"])
    if row.get("ai_verdict"):
        f["AI判定"] = str(row["ai_verdict"])[:300]
    for col, key in (("入队时间", "discovered_at"), ("更新时间", "updated_at")):
        ms = _iso_to_ms(row.get(key))
        if ms:
            f[col] = ms
    if row.get("pr_url"):
        f["PR链接"] = {"text": "PR", "link": str(row["pr_url"])}
    if row.get("note"):
        f["备注"] = str(row["note"])[:500]
    return f


def sync_rows(root: Path, rows: list[dict], *, force: bool = False) -> dict:
    """把 PR 行同步进「PR 记录」表。见 sync_table()。"""
    return sync_table(root, rows, table_name=TABLE_NAME, fields=FIELDS,
                      to_fields=row_to_fields, binding_key="pr_table_id",
                      force=force)


def sync_table(root: Path, rows: list[dict], *, table_name: str, fields: list,
               to_fields, binding_key: str, force: bool = False) -> dict:
    """把行 upsert 进指定表；表和字段不存在就建。返回计数摘要。

    同一个 base 里可挂多张表（PR 记录 / 仓库池 / 候选池…），每张表用自己的
    binding_key 缓存 table_id。键列恒为"键"（repo#number 或 repo 本身）。
    """
    binding = load_binding(root)
    app_token = _binding_app_token(root)
    if not app_token:
        raise RuntimeError(
            "no bitable app_token — 设 FEISHU_BITABLE_APP_TOKEN，或把表格链接"
            "里的 token 存进 state/feishu_bitable.json")
    table_id = binding.get(binding_key) or find_or_create_table(
        app_token, table_name, fields)
    save_binding(root, {"app_token": app_token, binding_key: table_id})
    ensure_columns(app_token, table_id, fields or [])
    # 转换器可能产出规格里漏写的列（2026-11-05：候选池行带「链接」而
    # CAND_FIELDS 没定义它，整批 batch_create 被 FieldNameNotFound 打回）。
    # 以实际产出的键为准补齐文本列，避免"代码改了一处、表建不出来"。
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
        key = fields["键"]
        hit = existing.get(key)
        if hit is None:
            to_create.append({"fields": fields})
        elif force or _fingerprint(hit.get("fields") or {}) != _fingerprint(fields):
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
    if rows:
        incoming = {f["键"] for f in all_fields}
        stale = [hit["record_id"] for key, hit in existing.items()
                 if key not in incoming]
        for i in range(0, len(stale), 500):
            _post("records/batch_delete", {"records": stale[i:i + 500]})
        pruned = len(stale)

    return {"created": len(to_create), "updated": len(to_update),
            "skipped": len(rows) - len(to_create) - len(to_update),
            "pruned": pruned}
