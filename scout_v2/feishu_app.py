"""Feishu application authentication and API access for live tables."""
from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path

_TOKEN_CACHE: tuple[float, str] = (0.0, "")
_TOKEN_TTL = 100 * 60          # tenant_access_token 有效期 2h，提前 20min 刷新

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
