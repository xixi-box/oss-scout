"""仓库评分：把三个指标合成一个总分，作为池子的准入与排序依据。

三个指标各自回答一个问题：
  外部合并率   —— 外部贡献**能不能**被合（0% 意味着写得多好都白费）
  中位合并耗时 —— 一次往返要多久（决定我们能不能在一周内迭代完）
  外部 PR/周   —— 这个仓库每年有多少次机会（也间接反映 maintainer 是否真的在看）

为什么不是一个线性加权：
  - 合并率是**门槛性质**，不是线性收益：0% 和 10% 的差距远大于 80% 和 90%
  - 耗时是**指数衰减**的：24h 和 48h 的体感差距，远大于 240h 和 264h
  - 量用 log 压缩：每周 1 个和 10 个有区别，10 个和 50 个区别不大
"""
from __future__ import annotations

import math

# 权重：合并率 45 / 速度 35 / 机会量 20
# 合并率最高是因为它决定"有没有意义"，速度其次是因为它决定"能不能迭代"，
# 量最低是因为小而快的仓库（pgvector 每周 0.1 个、88%、11h）依然值得做。
W_SHARE = 45.0
W_SPEED = 35.0
W_VOLUME = 20.0


def share_score(share_pct: float) -> float:
    """外部合并率 → 0..45 分。0% 直接归零（写得好也合不了）。"""
    pct = max(0.0, min(100.0, float(share_pct)))
    if pct <= 0:
        return 0.0
    # 平方根让中段更敏感：20% 拿 45*sqrt(0.2)=20 分，已经过半；
    # 50% 拿 32 分；80% 拿 40 分。
    return W_SHARE * math.sqrt(pct / 100.0)


def speed_score(median_hours: float | None) -> float:
    """中位合并耗时 → 0..35 分。无数据按最慢处理（0 分）。"""
    if median_hours is None:
        return 0.0
    h = max(0.0, float(median_hours))
    # 指数衰减，半衰期设在 1 周：24h→约 31 分，168h→约 17.5 分，720h→约 4 分
    # 用 24h 作为"满分基准"，超过就衰减。
    if h <= 24:
        return W_SPEED
    return W_SPEED * math.exp(-math.log(2) * (h - 24) / 168.0)


def volume_score(per_week: float) -> float:
    """外部 PR/周 → 0..20 分。log 压缩，10 个/周封顶。"""
    pw = max(0.0, float(per_week))
    if pw <= 0:
        return 0.0
    return W_VOLUME * min(1.0, math.log10(1.0 + pw) / math.log10(11.0))


def total(share_pct: float, median_hours: float | None, per_week: float) -> float:
    """合成总分 0..100。"""
    return round(share_score(share_pct) + speed_score(median_hours)
                 + volume_score(per_week), 1)


def grade(score: float) -> str:
    """分档，用于决定是否进开放池。"""
    if score >= 70:
        return "A"      # 优先做
    if score >= 50:
        return "B"      # 可做
    if score >= 30:
        return "C"      # 只观察
    return "D"          # 不入池


def explain(share_pct: float, median_hours: float | None, per_week: float) -> str:
    s = share_score(share_pct)
    v = speed_score(median_hours)
    q = volume_score(per_week)
    return (f"share={s:.1f} speed={v:.1f} volume={q:.1f} "
            f"total={s + v + q:.1f}")
