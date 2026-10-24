"""fixtures：旧结构样本（取自真实 state 文件的形态）。"""

# 旧 worklog 行（混合了 claim/非 claim 动作；ts 为裸本地时间）
LEGACY_WORKLOG_LINES = [
    '{"ts": "2026-09-16T21:51:48", "action": "manual_trigger", "detail": "demo run"}',
    '{"ts": "2026-09-16T21:51:48", "action": "segment1_monitor", "result": "no pr_open entries"}',
    '{"ts": "2026-10-24T18:28:59", "action": "claim_window_deferred", "result": "tool built"}',
    '{"ts": "2026-10-24T20:14:57", "action": "paused_by_user", "result": "stopped mid-run"}',
    'not-json-at-all',  # 历史账本容错：坏行跳过
]

# 旧 candidates.json 条目（含 queued_hist 重建条目）
LEGACY_QUEUE_ENTRY = {
    "id": "pydantic/pydantic-ai#7713",
    "repo": "pydantic/pydantic-ai",
    "number": 7713,
    "title": "Sample gated issue",
    "url": "https://github.com/pydantic/pydantic-ai/issues/7713",
    "lane": "gated",
    "score": 7,
    "class": "A",
    "blockers": [],
    "signals": {"maintainer_confirm": True},
    "created_at": "2026-10-20T10:00:00Z",
    "discovered_at": "2026-10-22T08:00:00",
    "status": "queued_hist",
    "lease": None,
}
