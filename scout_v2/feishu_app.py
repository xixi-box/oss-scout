"""飞书自建应用 API：发交互卡片（可编辑、可回调提交）+ tenant token。

与 notify.py（webhook 单向推送）的分工：
  webhook   只能推消息；卡片按钮收不到点击事件——自定义机器人没有应用身份
  本模块    自建应用身份（FEISHU_APP_ID/SECRET），可发**可交互卡片**（内含
            多行输入框 + 提交按钮），点击事件回调到 /feishu/callback，
            实现"卡片里改评论 → 一键发到 GitHub"

用户侧配置（一次性，5 分钟）：
  1. open.feishu.cn 创建自建应用（名字随意，如 oss-scout）
  2. 开通「机器人」能力；把应用拉进通知群
  3. 「事件与回调」→ 卡片回调地址填 https://oss.wangshun.work/feishu/callback
  4. 开通 im:message 权限（以应用身份发消息），发布应用
  5. 把 App ID / App Secret / Verification Token 交给运维配成环境变量

卡片动作回调安全：payload 带 verification token，回调接口与
FEISHU_VERIFICATION_TOKEN 比对；未配置 token 前回调一律 503 拒绝。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
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


def build_reply_card(repo: str, number: int, issue_title: str,
                     source_author: str, source_comment: str,
                     root_cause: str, draft: str) -> dict:
    """构造"可编辑回复"卡片：显示上下文 + 多行输入 + 一键提交。

    用卡片 1.0（`im/v1/messages` 的经典结构）：组件由 `tag` 声明，
    `type` 只表示按钮样式。form 内的按钮用 `action_type: form_submit`，
    点击后飞书回调 /feishu/callback，由回调端把编辑后的文本发到 GitHub。
    """
    excerpt = str(source_comment or "")[:400].replace("\n", " ")
    rc = str(root_cause or "")[:300]
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "title": {"tag": "plain_text",
                      "content": f"💬 回复草稿待审：{repo}#{number}"},
            "template": "orange",
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md",
                                    "content": (f"**仓库**：{repo}#{number}\n"
                                                f"**issue**：{issue_title}")}},
            {"tag": "div", "text": {"tag": "lark_md",
                                    "content": f"**我们的修法（根因）**：{rc or '见 PR 描述'}"}},
            {"tag": "div", "text": {"tag": "lark_md",
                                    "content": (f"**对方评论（{source_author}）**：\n"
                                                f"{excerpt}")}},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md",
                                    "content": "**AI 草拟的回复（可直接改，改完点发送）**"}},
            {"tag": "form", "name": "form_reply", "elements": [
                {"tag": "input", "name": "reply_text",
                 "default_value": str(draft)[:1500],
                 "placeholder": {"tag": "plain_text",
                                 "content": "在此修改回复内容"},
                 "max_length": 2000},
                {"tag": "button", "action_type": "form_submit",
                 "name": "send_reply",
                 "text": {"tag": "plain_text", "content": "一键发送到 GitHub"},
                 "type": "primary",
                 "value": {"do": "send", "repo": repo, "number": str(number)}},
            ]},
        ],
    }


def send_card_to_chat(chat_id: str, card: dict) -> tuple[bool, str]:
    tok = tenant_token()
    if not tok:
        return False, "no tenant token (check FEISHU_APP_ID/SECRET)"
    try:
        body = _request(
            "POST",
            f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
            {"receive_id": chat_id, "msg_type": "interactive", "content":
             json.dumps(card, ensure_ascii=False)}, token=tok)
        ok = body.get("code") == 0
        return ok, ("" if ok else str(body)[:300])
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:300]
