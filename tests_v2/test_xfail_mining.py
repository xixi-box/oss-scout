"""xfail/skip 挖掘的单元测试。

这是 bug hunter 的第二信号源：仓库自己承认的已知缺陷（带 reason 的
xfail/skip 标记）。密度高于任何静态规则，且自带复现方式。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scout_v2.workers.scan import mine_xfail  # noqa: E402


def _repo(tmp_path, files):
    for rel, body in files.items():
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body, encoding="utf-8")
    return tmp_path


class TestMineXfail:
    def test_reasoned_xfail_found(self, tmp_path):
        _repo(tmp_path, {"tests/test_a.py": '''
import pytest

@pytest.mark.xfail(reason="division by zero on empty input, issue #123")
def test_empty():
    assert 1 / 0
'''})
        hits = mine_xfail(tmp_path)
        assert len(hits) == 1
        assert hits[0]["rule_id"] == "xfail-marker/xfail"
        assert "#123" in hits[0]["message"]
        assert hits[0]["category"] == "correctness"

    def test_reasoned_skip_found(self, tmp_path):
        _repo(tmp_path, {"tests/test_b.py": '''
import unittest

class T(unittest.TestCase):
    @unittest.skip("broken since 2.0 upgrade, see #456")
    def test_x(self):
        pass
'''})
        hits = mine_xfail(tmp_path)
        assert len(hits) == 1
        assert hits[0]["rule_id"] == "xfail-marker/skip"

    def test_bare_skip_ignored(self, tmp_path):
        """无 reason 的 skip 多半是平台跳过（win32-only 之类），不是缺陷。"""
        _repo(tmp_path, {"tests/test_c.py": '''
import pytest

@pytest.mark.skip
def test_platform():
    pass
'''})
        assert mine_xfail(tmp_path) == []

    def test_non_test_files_ignored(self, tmp_path):
        _repo(tmp_path, {"src/app.py": '''
import pytest

@pytest.mark.xfail(reason="not in a test file")
def test_thing():
    pass
'''})
        assert mine_xfail(tmp_path) == []

    def test_cap_applied(self, tmp_path):
        body = "\n\n".join(
            f'@pytest.mark.xfail(reason="reason number {i}")\ndef test_{i}(): pass'
            for i in range(30))
        _repo(tmp_path, {"tests/test_many.py": body})
        hits = mine_xfail(tmp_path, max_findings=5)
        assert len(hits) == 5

    def test_fingerprints_differ_by_reason(self, tmp_path):
        _repo(tmp_path, {"tests/test_d.py": '''
import pytest

@pytest.mark.xfail(reason="first distinct reason")
def test_a():
    pass

@pytest.mark.xfail(reason="second distinct reason")
def test_b():
    pass
'''})
        hits = mine_xfail(tmp_path)
        assert len(hits) == 2
        assert hits[0]["fingerprint"] != hits[1]["fingerprint"]
