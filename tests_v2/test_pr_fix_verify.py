"""pr-fix 的验证闸：认不出怎么测就不许推。

2026-11-04 实录：docker/compose 的测试改动被手推上 fork，但上游 CI 不为
fork PR 自动跑（需维护者批准），所以「测试通过」没人验证过。自动化必须
自己验，验不了就不推——推一个未验证的补丁到别人 PR 上比不推更糟。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scout_v2.workers import pr_fix  # noqa: E402


class TestVerify:
    def test_no_changes_is_trusted(self, tmp_path):
        ok, desc = pr_fix._verify(tmp_path, [], {})
        assert ok is True
        assert desc == "no-change"

    def test_unknown_repo_is_not_pushed(self, tmp_path):
        """认不出测试运行器 → unverified → 调用方不得推送。"""
        (tmp_path / "README.md").write_text("hi", encoding="utf-8")
        ok, desc = pr_fix._verify(tmp_path, [" M README.md"], {})
        assert ok is False
        assert "unverified" in desc

    def test_go_package_is_run(self, tmp_path, monkeypatch):
        (tmp_path / "go.mod").write_text("module x\n\ngo 1.21\n", encoding="utf-8")
        calls = []

        def fake_run(cmd, cwd, timeout=1500):
            calls.append(cmd)
            return 0, "ok"

        monkeypatch.setattr(pr_fix, "_run", fake_run)
        ok, desc = pr_fix._verify(tmp_path, [" M pkg/watch/x.go"], {})
        assert ok is True
        assert calls and calls[0][0] == "go"
        assert "./pkg/watch/" in calls[0]

    def test_go_failure_blocks_push(self, tmp_path, monkeypatch):
        (tmp_path / "go.mod").write_text("module x\n", encoding="utf-8")
        monkeypatch.setattr(pr_fix, "_run", lambda *a, **k: (1, "--- FAIL: TestX"))
        ok, desc = pr_fix._verify(tmp_path, [" M pkg/watch/x.go"], {})
        assert ok is False
        assert "rc=1" in desc
        # 失败日志落盘，方便人看为什么没推
        assert (tmp_path.parent / "prfix-test.log").exists()

    def test_python_falls_back_to_changed_modules(self, tmp_path, monkeypatch):
        (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        calls = []

        def fake_run(cmd, cwd, timeout=1500):
            calls.append(cmd)
            return 0, "ok"

        monkeypatch.setattr(pr_fix, "_run", fake_run)
        ok, _ = pr_fix._verify(tmp_path, [" M src/mod.py"], {})
        assert ok is True
        assert "src/mod.py" in calls[0]

    def test_python_prefers_failing_nodes(self, tmp_path, monkeypatch):
        (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        calls = []
        monkeypatch.setattr(pr_fix, "_run",
                            lambda cmd, cwd, timeout=1500: (calls.append(cmd), (0, ""))[1])
        pr_fix._verify(tmp_path, [" M src/mod.py"],
                       {"failing": [{"name": "tests/test_a.py::TestX::test_y"}]})
        assert "tests/test_a.py" in calls[0]

    def test_missing_toolchain_is_not_pass(self, tmp_path, monkeypatch):
        """go 没装（rc=127）不能当成通过。"""
        (tmp_path / "go.mod").write_text("module x\n", encoding="utf-8")
        monkeypatch.setattr(pr_fix, "_run",
                            lambda *a, **k: (127, "go not installed"))
        ok, desc = pr_fix._verify(tmp_path, [" M a.go"], {})
        assert ok is False
        assert "rc=127" in desc
