"""Feishu application authentication and API access for live tables."""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

_TOKEN_CACHE: tuple[float, str] = (0.0, "")
_TOKEN_LOCK = threading.Lock()
_TOKEN_ERRORS = frozenset({99991663, 99991665})
_LOG = logging.getLogger(__name__)

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
    # Long-running syncs may retain a token across pages and batches.
    if token and _TOKEN_CACHE[1]:
        token = tenant_token()
    endpoint = urlsplit(url).path
    endpoint = re.sub(r"/(apps|tables|fields|records)/((?!batch_)[^/]+)",
                      r"/\1/{id}", endpoint)
    for attempt in range(2):
        status, body = _request_once(method, url, payload, token)
        code = body.get("code")
        if status < 400 and code in (0, None):
            return body
        # Only replay writes that Feishu explicitly rejected at authentication.
        retry = attempt == 0 and bool(token) and type(code) is int and code in _TOKEN_ERRORS
        detail = (f"Feishu {method} {endpoint} HTTP {status} "
                  f"code={code if type(code) is int else 'unavailable'}")
        _LOG.warning("%s%s", detail, "; refreshing credential and retrying" if retry else "")
        if retry:
            token = _tenant_token(rejected=token)
        elif status >= 400:
            raise urllib.error.HTTPError(endpoint, status, detail, None, None)
        else:
            return body
    raise AssertionError("unreachable Feishu request retry")


def _request_once(method: str, url: str, payload: dict | None,
                  token: str | None) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") \
        if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        with exc:
            try:
                body = json.loads(exc.read(64 * 1024))
            except (ValueError, AttributeError):
                body = {}
        return exc.code, body if isinstance(body, dict) else {}


def tenant_token() -> str:
    """Return the current Feishu tenant credential."""
    return _tenant_token()


def _tenant_token(*, rejected: str | None = None) -> str:
    global _TOKEN_CACHE
    with _TOKEN_LOCK:
        refresh_at, token = _TOKEN_CACHE
        started = time.monotonic()
        if token and token != rejected and started < refresh_at:
            return token
        _TOKEN_CACHE = (0.0, "")
        app_id, app_secret = _app_credentials()
        body = _request("POST", "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                        {"app_id": app_id, "app_secret": app_secret})
        code = body.get("code")
        token = body.get("tenant_access_token")
        if code not in (0, None) or not isinstance(token, str) or not token:
            raise RuntimeError(f"Feishu tenant_access_token/internal rejected credential request "
                               f"(code={code if type(code) is int else 'unavailable'})")
        lifetime = body.get("expire", body.get("expires_in"))
        if type(lifetime) is int and lifetime > 0:
            _TOKEN_CACHE = (started + lifetime - min(60, lifetime / 10), token)
        else:
            _LOG.warning("Feishu POST /open-apis/auth/v3/tenant_access_token/internal missing valid expiry")
        return token
