"""publish 的补丁卫生：构建垃圾路径整段丢弃，真实改动原样保留。

ClickHouse#129709 实录：验证阶段 uv/pip 在 src/ 下生成 UNKNOWN.egg-info，
被打进 change.patch 推到上游 PR，maintainer 点名移除。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scout_v2.workers.publish_remote import filter_patch_junk  # noqa: E402

REAL = """diff --git a/src/Core/MySQL/PacketsGeneric.cpp b/src/Core/MySQL/PacketsGeneric.cpp
index 111..222 100644
--- a/src/Core/MySQL/PacketsGeneric.cpp
+++ b/src/Core/MySQL/PacketsGeneric.cpp
@@ -1,3 +1,4 @@
+size_t result = 1;
"""

JUNK_EGG = """diff --git a/src/UNKNOWN.egg-info/PKG-INFO b/src/UNKNOWN.egg-info/PKG-INFO
new file mode 100644
index 000..111
--- /dev/null
+++ b/src/UNKNOWN.egg-info/PKG-INFO
+Metadata
"""

JUNK_PYCACHE = JUNK_EGG.replace("UNKNOWN.egg-info/PKG-INFO", "pkg/__pycache__/x.cpython-312.pyc")


def test_junk_sections_are_dropped_real_change_kept():
    mixed = REAL + JUNK_EGG + JUNK_PYCACHE
    out = filter_patch_junk(mixed)
    assert "PacketsGeneric.cpp" in out
    assert "egg-info" not in out
    assert "__pycache__" not in out


def test_only_junk_patch_becomes_empty():
    out = filter_patch_junk(JUNK_EGG)
    assert not out.strip()


def test_plain_patch_passes_through_untouched():
    assert filter_patch_junk(REAL) == REAL


def test_header_before_first_diff_is_kept():
    head = "index line\nold mode\n" + REAL
    assert filter_patch_junk(head).startswith("index line")
