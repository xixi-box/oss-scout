"""飞书 webhook 探针（只读发送一条测试卡片）。

用法：python /app/scripts/feishu_probe.py   # 在容器内执行
"""
import sys
import os
from pathlib import Path

sys.path.insert(0, "/app")
from scout_v2.feishu_bitable import pr_table_url  # noqa: E402
from scout_v2.notify import feishu_send_card  # noqa: E402

ok = feishu_send_card(
    "✅ 飞书通知接入成功",
    "oss-scout 流水线通知已就绪。之后这些事件会推到这里：\n"
    "· **回复草稿待人审**（通过飞书审批卡确认）\n"
    "· CI 挂红并已派自动修复\n"
    "· 新 PR 开出 / 候选升级转人工",
    button_text="打开飞书 PR 记录",
    button_url=pr_table_url(Path(os.environ.get("OSSCOUT_DASHBOARD_ROOT", "/data"))),
    force=True,
)
print("sent:", ok)
