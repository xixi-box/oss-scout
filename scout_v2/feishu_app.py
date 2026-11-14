"""Feishu application authentication and API access for live tables."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

_TOKEN_CACHE: tuple[float, str] = (0.0, "")
_TOKEN_TTL = 100 * 60          # tenant_access_token 有效期 2h，提前 20min 刷新

# 卡片里展示对方评论的上限。飞书单条消息有体积上限，但这里取的是一个
# 远高于常见评审长度的值——真实 review 通常 300~1500 字，宁可多显示也不要
# 在 400 字处静默切断（那会让人以为对方只说了半句）。
_COMMENT_SHOWN = 2000


def _app_credentials() -> tuple[str, str]:
    app_id = (os.environ.get("FEISHU_APP_ID") or "").strip()
    app_secret = (os.environ.get("FEISHU_APP_SECRET") or "").strip()
    if app_id and app_secret:
        return app_id, app_secret
    # 文件兜底：/data/state/feishu_app.json {"app_id":..., "app_secret":...}
    data_root = os.environ.get("OSSCOUT_DASHBOARD_ROOT", "/data")
    for p in (Path(data_root) / "state" / "feishu_app.json",
              Path(__file__).resolve().parents[1] / "state" / "feishu_app.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            return str(d.get("app_id") or ""), str(d.get("app_secret") or "")
        except Exception:  # noqa: BLE001
            continue
    return "", ""


def _verification_token() -> str:
    tok = (os.environ.get("FEISHU_VERIFICATION_TOKEN") or "").strip()
    if tok:
        return tok
    data_root = os.environ.get("OSSCOUT_DASHBOARD_ROOT", "/data")
    try:
        d = json.loads((Path(data_root) / "state" / "feishu_app.json")
                       .read_text(encoding="utf-8"))
        return str(d.get("verification_token") or "")
    except Exception:  # noqa: BLE001
        return ""


def app_configured() -> bool:
    app_id, app_secret = _app_credentials()
    return bool(app_id and app_secret)


def _request(method: str, url: str, payload: dict | None = None,
             token: str | None = None) -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") \
        if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def tenant_token() -> str:
    """tenant_access_token（进程内缓存，提前 20 分钟过期）。"""
    global _TOKEN_CACHE
    ts, tok = _TOKEN_CACHE
    if tok and time.time() - ts < _TOKEN_TTL:
        return tok
    app_id, app_secret = _app_credentials()
    body = _request("POST", "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                    {"app_id": app_id, "app_secret": app_secret})
    tok = str(body.get("tenant_access_token") or "")
    _TOKEN_CACHE = (time.time(), tok)
    return tok


def list_chats() -> tuple[list[dict], str]:
    """机器人所在的群列表。返回 (items, error)——error 非空说明查不到原因。

    常见错误是缺 `im:chat:readonly` 权限（应用未开通时 99991672）。把原因
    带出去，免得"发不出卡片"被误判成"机器人和群不存在"。
    """
    tok = tenant_token()
    if not tok:
        return [], "no tenant token (check FEISHU_APP_ID/SECRET)"
    try:
        body = _request("GET",
                        "https://open.feishu.cn/open-apis/im/v1/chats?page_size=50",
                        token=tok)
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            detail = {}
        return [], f"HTTP {e.code}: {detail.get('msg') or e.reason}"
    except Exception as e:  # noqa: BLE001
        return [], str(e)[:200]
    if body.get("code") not in (0, None):
        return [], f"code={body.get('code')} {body.get('msg')}"
    return list(body.get("data", {}).get("items") or []), ""


def find_group_chat_id() -> str | None:
    """找到机器人所在的第一个群（可配 FEISHU_CHAT_ID 固定指定）。

    FEISHU_CHAT_ID 优先——设了它就不需要 `im:chat:readonly` 权限，
    只留发消息的 `im:message`。
    """
    fixed = (os.environ.get("FEISHU_CHAT_ID") or "").strip()
    if fixed:
        return fixed
    items, _err = list_chats()
    return str(items[0]["chat_id"]) if items else None
