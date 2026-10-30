"""pytest 配置：把 tests_v2/fixtures 加进 sys.path。

fixtures/legacy_samples.py 被 TestWorklogCompat 直接 `from legacy_samples import ...`，
依赖 pytest 的 rootdir 插入行为。单独跑本文件时那层插入不一定存在，测试会
ModuleNotFoundError（2026-10-30 实测）。显式声明路径，让单文件跑和全量跑一致。
"""
import sys
from pathlib import Path

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
if str(_FIXTURES) not in sys.path:
    sys.path.insert(0, str(_FIXTURES))
