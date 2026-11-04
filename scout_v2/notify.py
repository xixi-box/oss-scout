"""飞书自定义机器人通知。

用法（任何模块）：from ..notify import feishu_send; feishu_send("标题", "正文")

webhook 来源（按序取第一个非空）：
  1. 环境变量 FEISHU_WEBHOOK_URL
  2. $OSSCOUT_DASHBOARD_ROOT/state/feishu_webhook.txt（默认 /data/state/...）
  3. 仓库本地 state/feishu_webhook.txt

机器人创建（飞书群设置 → 群机器人 → 添加自定义机器人）会给出一个
https://open.feishu.cn/open-apis/bot/v2/hook/xxx 形式的 URL，粘进上面任一
位置即可。没有配置时 feishu_send 静默返回 False——通知永远不能影响主流程。

限流保护：模块级 5 秒冷却（飞书自定义机器人上限 100 条/分钟，突发密集事件
如 PR watch 扫描可能连发多条，冷却兜底）。
"""
from __future__ import annotations

import os
import time
from pathlib import Path

_LAST_SENT = 0.0
_COOLDOWN_S = 5.0


def _webhook_url() -> str:
    url = (os.environ.get("FEISHU_WEBHOOK_URL") or "").strip()
    if url:
        return url
    candidates = []
    data_root = os.environ.get("OSSCOUT_DASHBOARD_ROOT", "/data")
    candidates.append(Path(data_root) / "state" / "feishu_webhook.txt")
    candidates.append(Path(__file__).resolve().parents[1] / "state" / "feishu_webhook.txt")
    for c in candidates:
        try:
            url = c.read_text(encoding="utf-8").strip()
            if url:
                return url
        except OSError:
            continue
    return ""


def feishu_send(title: str, text: str = "", *, force: bool = False) -> bool:
    """发一条飞书文本消息。返回是否真的发出（未配置/失败 → False）。

    force=True 跳过冷却（用于明确的一次性事件如"已发送回复"）。
    任何异常都被吞掉：通知失败绝不影响主流程。
    """
    global _LAST_SENT
    url = _webhook_url()
    if not url:
        return False
    if not force and (time.monotonic() - _LAST_SENT) < _COOLDOWN_S:
        return False
    try:
        import json as _json
        import urllib.request

        payload = _json.dumps({
            "msg_type": "text",
            "content": {"text": f"{title}\n{text}".strip()},
        }, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload, method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = _json.loads(resp.read().decode("utf-8", "replace"))
            ok = body.get("code") in (0, None) or body.get("StatusCode") == 0
            _LAST_SENT = time.monotonic()
            return bool(ok)
    except Exception:  # noqa: BLE001 — 通知失败不影响主流程
        return False
