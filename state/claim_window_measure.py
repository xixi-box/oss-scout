"""认领窗口测量 v3（REST all-state issues 60d + timeline 首个外部信号）。
在稳定网络窗口运行：python state/claim_window_measure.py
输出 state/claim_window.json。"""
# 完整实现见会话记录 2026-10-19；REST 版参数：
# repos/{R}/issues?state=all&since=<60d>&per_page=30 → timeline → 首个外部 cross-ref-PR 或 assigned
