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

# 看板公网入口（飞书卡片按钮跳转用）。可用环境变量覆盖。
DASHBOARD_URL = (os.environ.get("DASHBOARD_PUBLIC_URL")
                 or "https://oss.wangshun.work").rstrip("/")


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


def feishu_send_card(header: str, md_body: str, *,
                     button_text: str | None = None,
                     button_url: str | None = None,
                     template: str = "blue", force: bool = False) -> bool:
    """发一条交互卡片（支持 lark_md 与 URL 跳转按钮）。

    webhook 自定义机器人只能处理 URL 跳转按钮（回调按钮需要开放平台应用），
    所以"点击直达"的形态是：点按钮 → 打开看板对应页面 → 人在看板上完成动作。
    失败回退为纯文本消息（保证消息可达），再失败才返回 False。
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

        elements: list[dict] = [{"tag": "div",
                                 "text": {"tag": "lark_md", "content": md_body}}]
        if button_text and button_url:
            elements.append({"tag": "action", "actions": [{
                "tag": "button",
                "text": {"tag": "plain_text", "content": button_text},
                "type": "primary",
                "url": button_url,
            }]})
        payload = _json.dumps({
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {"template": template,
                           "title": {"tag": "plain_text", "content": header}},
                "elements": elements,
            },
        }, ensure_ascii=False).encode("utf-8")

        req = urllib.request.Request(
            url, data=payload, method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = _json.loads(resp.read().decode("utf-8", "replace"))
            ok = (body.get("code") in (0, None)
                  or body.get("StatusCode") == 0
                  or body.get("msg") in ("success", None))
            _LAST_SENT = time.monotonic()
            if not ok:
                # 卡片被拒（旧版 bot 等）→ 回退纯文本，至少消息可达
                return feishu_send(header, md_body, force=True)
            return True
    except Exception:  # noqa: BLE001 — 通知失败不影响主流程
        try:
            return feishu_send(header, md_body, force=True)
        except Exception:  # noqa: BLE001
            return False
