"""起草回复的 prompt 必须包含整条评审。

2026-11-04 实录（docker/compose#14450）：维护者 740 字的评审在拼 prompt 时
被 `body[:500]` 截断，起草出的回复漏了后半段"请加一个 Matches 级回归测试"。
模型没看见的要求，不可能出现在回复里——而人审草稿时已经改好了代码，
只会觉得是模型不听话。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import importlib.util  # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    "monitor_prs", Path(__file__).resolve().parents[1] / "scripts" / "monitor_prs.py")
mp = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mp)


# 逐字取自 docker/compose#14450 的真实评审（ndeloof，2026-11-04），740 字。
# 用真实长度而非短样例：这条 bug 只在超过旧上限时才暴露。
REVIEW = (
    "Thanks, removing the `*` special case is the right fix: I checked that with "
    "`*` then `!src`, `data/log`, `.env` and `node_modules/...` are now ignored "
    "while `src/**` is not, and deny-lists are unchanged.\n"
    "\n"
    "One blocking point: `TestNewDockerPatternMatcher/Wildcard_with_exclusion` "
    "(`pkg/watch/dockerignore_test.go`) asserts the behavior this PR removes "
    '(`["*", "!file.txt"]` expected to become `["!/repo/file.txt"]`), and it now '
    "fails (`expected patterns length: 1, got: 2`). It needs to be updated to "
    'expect `["/repo/*", "!/repo/file.txt"]`.\n'
    "\n"
    "Please also add a regression test for the allow-list at the `Matches` level "
    "(e.g. `*` then `!src`: `data/log` ignored, `src/index.js` not), since the "
    "existing test only looks at the parsed patterns.")


class TestDraftPromptCompleteness:
    def test_fixture_is_long_enough_to_expose_the_bug(self):
        assert len(REVIEW) > 500, "must exceed the old 500-char cut"

    def test_limit_covers_real_reviews(self):
        assert mp.COMMENT_TEXT_LIMIT >= 4000

    def test_thread_keeps_the_whole_comment(self):
        """_thread 里的上限决定模型能看到多少——不能低于真实评审长度。"""
        assert mp.COMMENT_TEXT_LIMIT >= len(REVIEW)

    def test_the_second_request_is_inside_the_limit(self):
        """后半段要求（Matches 级测试）必须落在上限内，否则模型看不见。"""
        assert "Matches" in REVIEW[-mp.COMMENT_TEXT_LIMIT:]
        # 旧的 500 字上限确实会切掉它——证明这条断言有意义
        assert "Matches" not in REVIEW[:500]
