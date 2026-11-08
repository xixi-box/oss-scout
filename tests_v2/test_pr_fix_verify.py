"""pr-fix 的验证闸：认不出怎么测就不许推。

2026-11-04 实录：docker/compose 的测试改动被手推上 fork，但上游 CI 不为
fork PR 自动跑（需维护者批准），所以「测试通过」没人验证过。自动化必须
自己验，验不了就不推——推一个未验证的补丁到别人 PR 上比不推更糟。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scout_v2.workers import pr_fix  # noqa: E402


def test_sha_only_review_skips_agent(tmp_path, monkeypatch, capsys):
    fix = tmp_path / "fix.json"
    context = tmp_path / "context.json"
    fix.write_text(json.dumps({"kind": "followup", "review_id": 1}))
    context.write_text(json.dumps({"targets": {"review:1": {"body": "a" * 40}}}))
    monkeypatch.setattr(sys, "argv", ["pr_fix", "--fix", str(fix), "--context", str(context),
                                      "--repo-dir", str(tmp_path)])
    monkeypatch.setattr(pr_fix, "OpenCodeClient",
                        lambda **kwargs: (_ for _ in ()).throw(AssertionError("agent called")))
    assert pr_fix.main() == 0
    assert "commit SHA only" in capsys.readouterr().out


class TestVerify:
    def test_node_runtime_uses_node_test_runner_even_with_python_config(self, tmp_path, monkeypatch):
        (tmp_path / "pyproject.toml").touch()
        commands = []
        monkeypatch.setattr(pr_fix, "_run",
                            lambda cmd, cwd, timeout=1500: (commands.append(cmd), (0, ""))[1])
        ok, detail = pr_fix._verify(tmp_path, [" M lib/fs.js", " M test/parallel/test-fs.js"],
                                    {"upstream_repo": "nodejs/node"})
        assert ok and "tools/test.py" in detail
        assert [cmd[0] for cmd in commands] == ["./configure", "make", "python3"]
        assert commands[-1][1:] == ["tools/test.py", "test/parallel/test-fs.js"]

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


class TestGoTagsRetry:
    """Go 仓库的测试套件常要构建标签才跑得全——不带标签的失败必须先
    带仓库惯用标签重试再下结论（milvus -tags dynamic,test 实录：
    不带 rc=1，带上全绿，差点误杀一个真修复）。"""

    def test_retries_with_ci_detected_tags(self, tmp_path, monkeypatch):
        (tmp_path / "go.mod").write_text("module x\n", encoding="utf-8")
        wf = tmp_path / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "ci.yml").write_text(
            "  run: go test -tags dynamic,test ./pkg/util/retry/",
            encoding="utf-8")
        (tmp_path / "pkg" / "watch").mkdir(parents=True)
        calls = []

        def fake_run(cmd, cwd, timeout=1500):
            calls.append(cmd)
            # 不带标签 rc=1；带标签 rc=0（命令里任一元素含 -tags 即视为带标签）
            tagged = any("-tags" in c for c in cmd)
            return (0 if tagged else 1), "ok"

        monkeypatch.setattr(pr_fix, "_run", fake_run)
        ok, desc = pr_fix._verify(tmp_path, [" M pkg/watch/x.go"], {})
        assert ok is True
        assert any("-tags=dynamic,test" in " ".join(c) for c in calls)

    def test_fails_when_tagged_retry_also_fails(self, tmp_path, monkeypatch):
        (tmp_path / "go.mod").write_text("module x\n", encoding="utf-8")
        monkeypatch.setattr(pr_fix, "_run",
                            lambda cmd, cwd, timeout=1500: (1, "still failing"))
        ok, desc = pr_fix._verify(tmp_path, [" M a.go"], {})
        assert ok is False

    def test_no_tags_in_repo_means_no_retry(self, tmp_path, monkeypatch):
        (tmp_path / "go.mod").write_text("module x\n", encoding="utf-8")
        calls = []
        monkeypatch.setattr(pr_fix, "_run",
                            lambda cmd, cwd, timeout=1500:
                            (calls.append(cmd), (1, "x"))[1])
        ok, _ = pr_fix._verify(tmp_path, [" M a.go"], {})
        assert ok is False
        assert len(calls) == 1   # 没有可用的标签，不瞎重试


class TestNodeTargetedTests:
    """大仓库全量 npm test 动辄半小时——超时=未验证=拒推。改动的测试
    文件存在时优先 vitest/jest 定向跑（promptfoo 实录）。"""

    def test_vitest_targeted_on_changed_test_files(self, tmp_path, monkeypatch):
        pkg = {"devDependencies": {"vitest": "^1"}}
        (tmp_path / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
        calls = []
        monkeypatch.setattr(pr_fix, "_run",
                            lambda cmd, cwd, timeout=1500:
                            (calls.append(cmd), (0, ""))[1])
        ok, _ = pr_fix._verify(tmp_path, [" M src/x.ts", " A test/x.test.ts"], {})
        assert ok is True
        assert calls and calls[0][0] == 'npx' and calls[0][1] == '--yes'
        assert calls[0][2] == 'vitest'
        assert any("test/x.test.ts" in c for c in calls[0])

    def test_falls_back_to_npm_test_without_vitest(self, tmp_path, monkeypatch):
        (tmp_path / "package.json").write_text(
            json.dumps({"scripts": {"test": "mocha"}}), encoding="utf-8")
        calls = []
        monkeypatch.setattr(pr_fix, "_run",
                            lambda cmd, cwd, timeout=1500:
                            (calls.append(cmd), (0, ""))[1])
        ok, _ = pr_fix._verify(tmp_path, [" M test/x.test.js"], {})
        assert ok is True
        assert calls and calls[0][0] == "npm"


class TestPytestNoTestsCollected:
    def test_rc5_falls_back_to_tests_dir(self, tmp_path, monkeypatch):
        (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        (tmp_path / "tests").mkdir()
        calls = []

        def fake_run(cmd, cwd, timeout=1500):
            calls.append(cmd)
            rc = 5 if "tests/" not in cmd else 0   # 第一次没收集到，tests/ 全过
            return rc, ""

        monkeypatch.setattr(pr_fix, "_run", fake_run)
        ok, desc = pr_fix._verify(tmp_path, [" M src/mod.py"], {})
        assert ok is True
        assert any("tests/" in " ".join(c) for c in calls), calls

    def test_rc5_without_tests_dir_is_unverified(self, tmp_path, monkeypatch):
        (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        calls = []
        monkeypatch.setattr(pr_fix, "_run",
                            lambda cmd, cwd, timeout=1500:
                            (calls.append(cmd), (5, ""))[1])
        ok, _ = pr_fix._verify(tmp_path, [" M src/mod.py"], {})
        assert ok is False
        assert len(calls) == 1
