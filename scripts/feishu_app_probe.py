"""飞书自建应用探针：实发一张"可编辑回复"卡片。

用法（容器内）：python /app/scripts/feishu_app_probe.py
只读操作：发一条消息，不改任何状态。
"""
import sys

sys.path.insert(0, "/app")

from scout_v2 import feishu_app  # noqa: E402

print("app_configured:", feishu_app.app_configured())
tok = feishu_app.tenant_token()
print("tenant_token:", "ok" if tok else "MISSING")

chats = feishu_app.find_group_chat_id()
print("chat_id:", chats or "NOT FOUND")

card = feishu_app.build_reply_card(
    repo="OpenHands/software-agent-sdk",
    number=6269,
    issue_title="fix: tool call arguments dropped when streaming is enabled",
    source_author="enyst",
    source_comment=(
        "Thanks for the patch. Could you add a regression test that covers the "
        "streaming path? Also the changelog entry seems to be missing."
    ),
    root_cause="streamed deltas bypass _merge_arguments, so partial JSON is "
               "appended instead of deep-merged",
    draft=(
        "Thanks for the review. The streaming path bypassed `_merge_arguments`, "
        "so each delta was appended rather than deep-merged into the pending "
        "tool call. I've added a regression test that streams a multi-chunk "
        "argument payload and asserts the merged result, plus the changelog "
        "entry you mentioned."
    ),
)

if not chats:
    print("cannot send: no chat id (is the bot in the group?)")
    raise SystemExit(1)

ok, err = feishu_app.send_card_to_chat(chats, card)
print("sent:", ok, err)
