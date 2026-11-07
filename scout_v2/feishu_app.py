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


def build_reply_card(repo: str, number: int, issue_title: str,
                     source_author: str, source_comment: str,
                     root_cause: str, draft: str,
                     *, fix_state: str = "none", pr_url: str = "",
                     source_kind: str = "issue", comment_id: int | None = None) -> dict:
    """构造"可编辑回复"卡片：上下文 + 多行输入 + 一键提交。

    用卡片 1.0（`im/v1/messages` 的经典结构）：组件由 `tag` 声明，
    `type` 只表示按钮样式。form 内的按钮用 `action_type: form_submit`，
    点击后飞书回调 /feishu/callback。

    fix_state 决定这张卡片的语义，也就决定了按钮做什么：
      "none"    只回复。按钮只发评论。
      "pending" 改动还在流水线里跑。按钮会等它完成再一起推。
      "ready"   改动已提交到暂存分支（**没动 PR 分支**）。点按钮 =
                发评论 + 把暂存提交快进到 PR 分支。人这一下就是唯一的放行点。
    """
    # 保留换行：评审内容是分条的（"One blocking point: ..." / "Please also ..."），
    # 压成一行后结构全丢，读的人要靠猜哪句是一条要求。
    # 2026-11-04 实录：740 字的评审被 [:400] 切掉后两条要求都不见了。
    raw = str(source_comment or "").strip()
    if len(raw) > _COMMENT_SHOWN:
        excerpt = (raw[:_COMMENT_SHOWN].rstrip()
                   + f"\n\n…（原文 {len(raw)} 字，此处显示前 {_COMMENT_SHOWN} 字，"
                     f"完整内容见 PR 页）")
    else:
        excerpt = raw
    rc = str(root_cause or "")[:300]

    link = pr_url or f"https://github.com/{repo}/pull/{number}"
    target = f"[{repo}#{number}]({link})"

    status_line = {
        "pending": "⏳ **代码修改进行中**，完成后这张卡片会更新",
        "pushed": "✅ **已修改并推送**（commit 已在 PR 上）",
        "nochange": "✅ **无需变更**（opencode 判断修复已在分支上，没有派发）",
        "failed": "⚠️ **代码修改未完成**（测试没过或无法验证，见 audit）",
    }.get(fix_state, "")
    # 代码修改一律自动推送，人只对"对外文字"负责——按钮永远只发回复。
    button_text = "一键发送回复"

    elements: list[dict] = [
        {"tag": "div", "text": {"tag": "lark_md",
                                "content": f"**仓库**：{target}\n"
                                           f"**issue**：{issue_title}"}},
        {"tag": "div", "text": {"tag": "lark_md",
                                "content": f"**我们的修法（根因）**：{rc or '见 PR 描述'}"}},
    ]
    if status_line:
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                                                "content": status_line}})
    elements += [
        {"tag": "div", "text": {"tag": "lark_md",
                                "content": (f"**对方评论（{source_author}）**：\n"
                                            f"{excerpt}")}},
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md",
                                "content": "**AI 草拟的回复（可直接改，改完点发送）**"}},
        {"tag": "form", "name": "form_reply", "elements": [
            {"tag": "input", "name": "reply_text",
             "default_value": str(draft)[:1000],
             # 单行输入框写评审回复太挤（实测 378 字的草稿只能看到开头一行）。
             # multiline_text + rows 给足可视高度，auto_resize 让长文本自己撑开。
             "input_type": "multiline_text",
             "rows": 6,
             "auto_resize": True,
             "max_rows": 12,
             "width": "fill",
             "placeholder": {"tag": "plain_text",
                             "content": "在此修改回复内容"},
             # 飞书 input 的 max_length 上限就是 1000，超了整张卡 400
             "max_length": 1000},
            {"tag": "button", "action_type": "form_submit",
             "name": "send_reply",
             "text": {"tag": "plain_text", "content": button_text},
             "type": "primary",
             # 回复去哪由被回复评论的来源决定：行内评论回行内线程，
             # issue 层评论进 issue 层——发错层会让对话断线。
             "value": {"do": "send", "repo": repo, "number": str(number),
                       "kind": source_kind or "issue",
                       "comment_id": str(comment_id or "")}},
        ]},
    ]

    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "title": {"tag": "plain_text",
                      "content": f"💬 回复待审：{repo}#{number}"},
            "template": "orange",
        },
        "elements": elements,
    }


def build_pr_approval_card(record: dict, *, draft: str = "", complete: bool = False,
                           error: str = "", preview: bool = False) -> dict:
    repo, number = record["repo"], record["number"]
    link = f"https://github.com/{repo}/pull/{number}"
    run_id = str(record.get("run_id") or "")
    sha = record.get("commit_sha") or ""
    source_url = str(record.get("source_url") or "")
    source_label = "原行内讨论" if record.get("source_kind") == "inline" else "原评论"
    elements = [{"tag": "div", "text": {"tag": "lark_md", "content":
                 f"[{repo}#{number}]({link})" +
                 (f" · [测试记录](https://github.com/xixi-box/oss-scout/actions/runs/{run_id})"
                  if run_id.isdigit() else "")}}]
    if source_url.startswith(link + "#"):
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                         "content": f"[{source_label}]({source_url})"}})
    if preview:
        elements.append({"tag": "div", "text": {"tag": "plain_text",
                         "content": "演示 · 按钮仅预览，不会向 GitHub 写入。"}})
    status = record.get("reply_status")
    for value in (
        record.get("title"),
        f"{record['source_author']}：{record.get('source_comment') or ''}" if record.get("source_author") else "",
        record.get("summary"),
        (f"{'已推送' if record.get('pushed') else '已提交，待推送'} {sha[:12]}\n"
         f"测试：{record.get('test_result') or ''}\n"
         + "\n".join(record.get("changed_files") or [])) if sha else "",
        (f"回复发送失败\n草稿：{draft}" if draft else "回复发送失败") if status == "failed" else "",
        (f"已回复：{draft}" if draft else "已回复") if status == "sent" else "",
        error,
    ):
        if value:
            elements.append({"tag": "div", "text": {"tag": "plain_text", "content": str(value)[:1200]}})
    if not complete:
        button = {"tag": "button", "type": "primary", "name": "confirm",
                  "text": {"tag": "plain_text", "content": (
                      "确认推送并回复" if sha and draft else "确认推送" if sha else "确认回复")},
                  "value": {"do": "preview_pr_followup" if preview else "approve_pr_followup",
                            "job_id": record.get("job_id", "")}}
        form = []
        if draft:
            form.append({"tag": "input", "name": "reply_text", "default_value": draft,
                         "input_type": "multiline_text", "rows": 6, "max_length": 1000,
                         "width": "fill", "placeholder": {"tag": "plain_text", "content": "编辑回复草稿"}})
        button["action_type"] = "form_submit"
        elements.append({"tag": "form", "name": "approval", "elements": form + [button]})
    return {"config": {"wide_screen_mode": True, "update_multi": True},
            "header": {"template": "red" if status == "failed" else "green" if complete else "orange",
                       "title": {"tag": "plain_text", "content":
                                 f"{'PR 跟进演示' if preview else 'PR 跟进结果' if complete else 'PR 跟进待确认'}：{repo}#{number}"}},
            "elements": elements}


def update_card_message(message_id: str, card: dict) -> tuple[bool, str]:
    """原地更新已发出的卡片（im/v1/messages PATCH）。

    点击发送后，飞书只会用回调响应里的卡片替换**那一条**消息；如果响应
    丢了（网络、超时），或者我们想让这张卡片在之后继续反映真实状态，
    就需要主动 PATCH 一次。卡片必须发出时带 update_multi=true。
    """
    if not message_id:
        return False, "no message_id"
    tok = tenant_token()
    if not tok:
        return False, "no tenant token"
    payload = {"content": json.dumps(card, ensure_ascii=False)}
    try:
        body = _request(
            "PATCH",
            f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}",
            payload, token=tok)
        ok = body.get("code") == 0
        return ok, ("" if ok else str(body)[:300])
    except urllib.error.HTTPError as e:
        try:
            parsed = json.loads(e.read().decode("utf-8", "replace"))
            return False, f"HTTP {e.code}: {parsed.get('msg') or ''}"[:300]
        except Exception:  # noqa: BLE001
            return False, f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:300]


def build_sent_card(repo: str, number: int, reply_text: str,
                    *, pr_url: str = "") -> dict:
    """"已回复"确认卡片：替换掉输入表单，留下发出去了什么。"""
    link = pr_url or f"https://github.com/{repo}/pull/{number}"
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "title": {"tag": "plain_text",
                      "content": f"✅ 已回复 {repo}#{number}"},
            "template": "green",
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md",
                                    "content": f"**仓库**：[{repo}#{number}]({link})"}},
            {"tag": "div", "text": {"tag": "lark_md",
                                    "content": "**已发出的回复**：\n"
                                               + str(reply_text)[:900]}},
        ],
    }


def send_card_to_chat(chat_id: str, card: dict, *, message_uuid: str = "") -> tuple[bool, str]:
    tok = tenant_token()
    if not tok:
        return False, "no tenant token (check FEISHU_APP_ID/SECRET)"
    try:
        message = {"receive_id": chat_id, "msg_type": "interactive",
                   "content": json.dumps(card, ensure_ascii=False)}
        if message_uuid:
            message["uuid"] = message_uuid
        body = _request(
            "POST",
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
            message, token=tok)
        ok = body.get("code") == 0
        return ok, (str((body.get("data") or {}).get("message_id") or "") if ok else str(body)[:300])
    except urllib.error.HTTPError as e:
        # 飞书把"卡片非法"的原因放在错误体里（如 max_length exceed）。
        # 只报 "HTTP Error 400" 等于让人再去猜一轮，这里把原因带出去。
        try:
            detail = e.read().decode("utf-8", "replace")
            parsed = json.loads(detail)
            return False, f"HTTP {e.code}: {parsed.get('msg') or detail[:200]}"
        except Exception:  # noqa: BLE001
            return False, f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:300]
