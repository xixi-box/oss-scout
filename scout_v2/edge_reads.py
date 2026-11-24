"""Read-only Cloudflare Worker transport; never sends publication credentials."""
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

import yaml

_LOG = logging.getLogger(__name__)
_LOCK = threading.Lock()
_DISCOVERY = {}
_BACKOFF = {}
_WORKER_HOST = re.compile(r"https://oss-scout-hunter\.[a-z0-9-]+\.workers\.dev/?\Z")
_ALLOWED = {"issue-context", "pr-snapshot", "repository-policy", "competition-search", "reference-fact", "feishu-record-index"}


class EdgeRateLimited(RuntimeError):
    def __init__(self, seconds):
        self.seconds = seconds
        super().__init__(f"edge GitHub API rate limited; retry after {seconds}s")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _read_json(request, timeout):
    with urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
        data = response.read(16 * 1024 * 1024 + 1)
        if len(data) > 16 * 1024 * 1024:
            raise ValueError("edge response too large")
        return json.loads(data)


def _config(root):
    try:
        cfg = yaml.safe_load((Path(root) / "configs/cloudflare.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, ValueError, yaml.YAMLError):
        return {}
    if not isinstance(cfg, dict) or cfg.get("edge_reads_enabled") is not True:
        return {}
    for env, key in (("CF_API_TOKEN", "api_token"), ("CF_ACCOUNT_ID", "account_id"),
                     ("CF_INGEST_TOKEN", "ingest_token"), ("CF_EDGE_URL", "edge_url")):
        if os.environ.get(env):
            cfg[key] = os.environ[env].strip()
    return cfg


def _endpoint(cfg):
    explicit = str(cfg.get("edge_url") or "").rstrip("/")
    if explicit:
        return explicit if _WORKER_HOST.fullmatch(explicit) else None
    account, api = str(cfg.get("account_id") or ""), cfg.get("api_token")
    if not re.fullmatch(r"[a-fA-F0-9]{32}", account) or not api:
        return None
    now = time.monotonic()
    with _LOCK:
        hit = _DISCOVERY.get(account)
        if hit and hit[0] > now:
            return hit[1]
    url = None
    try:
        request = urllib.request.Request(
            f"https://api.cloudflare.com/client/v4/accounts/{account}/workers/subdomain",
            headers={"Authorization": "Bearer " + api})
        result = _read_json(request, 8)
        name = result.get("result", {}).get("subdomain", "")
        if result.get("success") and isinstance(name, str) and re.fullmatch(r"[a-z0-9-]+", name):
            url = f"https://oss-scout-hunter.{name}.workers.dev"
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    with _LOCK:
        _DISCOVERY[account] = (now + (3600 if url else 300), url)
    return url


def edge_read(root, route, payload, *, feishu_token=None):
    """Return a complete edge response, or None for compatibility fallback.

    Rate limiting is surfaced rather than immediately retrying against GitHub.
    Old/unreachable Workers fall back for five minutes, including during rollout.
    """
    if route not in _ALLOWED:
        raise ValueError("unknown edge read route")
    cfg = _config(root)
    token = cfg.get("ingest_token")
    if not token:
        return None
    url = _endpoint(cfg)
    if not url:
        return None
    key = (url, route)
    now = time.monotonic()
    with _LOCK:
        if _BACKOFF.get(key, 0) > now:
            return None
    headers = {"Content-Type": "application/json", "X-Ingest-Token": token}
    if feishu_token is not None:
        if route != "feishu-record-index":
            raise ValueError("Feishu token restricted to business record index")
        headers["X-Feishu-Tenant-Token"] = feishu_token
    req = urllib.request.Request(
        f"{url}/internal/v1/{route}", data=json.dumps(payload).encode(),
        headers=headers, method="POST")
    try:
        result = _read_json(req, 90)
        if not isinstance(result, dict) or result.get("error"):
            raise ValueError("invalid edge response")
        return result
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            retry = exc.headers.get("Retry-After", "60")
            raise EdgeRateLimited(max(60, int(retry) if retry.isdecimal() else 60)) from exc
        reason = f"HTTP {exc.code}"
    except (OSError, ValueError, TypeError) as exc:
        reason = type(exc).__name__
    with _LOCK:
        _BACKOFF[key] = time.monotonic() + 300
    _LOG.warning("Cloudflare edge %s unavailable (%s); using local read fallback", route, reason)
    return None
