"""P5 测试：repair 证据链（base 失败→补丁→通过）、补丁内容安全检查、质量闸整合。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scout_v2.schemas import Job  # noqa: E402
from scout_v2.workers.repair import (check_patch_content,  # noqa: E402
                                     run_repair)


def make_repo(tmp_path: Path) -> tuple[Path, str]:
    """建一个含 planted bug 的仓库：div 用整除，测试要求真除。返回 (path, base_sha)。"""
    r = tmp_path / "repo"
    r.mkdir()
    def git(*a):
        import subprocess
        return subprocess.run(["git", *a], cwd=r, capture_output=True, text=True, check=True)
    git("init", "-q", "-b", "main")
    (r / "calc.py").write_text("def div(a, b):\n    return a // b\n", encoding="utf-8")
    (r / "test_div.py").write_text(
        "from calc import div\n\ndef test_div():\n    assert div(7, 2) == 3.5\n", encoding="utf-8")
    git("add", "-A")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "bug")
    sha = git("rev-parse", "HEAD").stdout.strip()
    return r, sha


def job(sha) -> Job:
    return Job(mode="repair", repo="xixi-box/oss-scout-v2-test", base_sha=sha,
               idempotency_key=f"repair:x:{sha}:fp")


GOOD_PATCH = """--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def div(a, b):
-    return a // b
+    return a / b
"""


def replay_plan(*, setup_steps=()):
    return {"setup_steps": list(setup_steps),
            "base_failure": ["test_div", "assert 3 == 3.5"],
            "patched_success": ["PASSED"]}


class TestRepairEvidenceChain:
    def test_inline_rust_regression_does_not_apply_fix_on_base(self, tmp_path, monkeypatch):
        from scout_v2.workers import repair

        repo, _ = make_repo(tmp_path)
        path = "aml.rs"
        source = "fn value() -> i32 { 0 }\n"
        tests = "#[cfg(test)]\nmod tests {\n}\n"
        (repo / path).write_text(source + tests, encoding="utf-8")
        repair._git(["add", path], repo)
        repair._git(["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "rust"], repo)
        sha = repair._git(["rev-parse", "HEAD"], repo).stdout.strip()
        new_tests = tests.replace("\n}", "\n    #[test] fn regression() { assert_eq!(super::value(), 1); }\n}")
        observed = []

        class Client:
            def run(self, model, prompt):
                (self.workdir / path).write_text(source.replace("{ 0 }", "{ 1 }") + new_tests, encoding="utf-8")
                (self.workdir / repair.RESULT_FILE).write_text(json.dumps({
                    "test_file": path, "test_cmd": "cargo test regression", "setup_steps": [],
                    "base_failure": ["regression", "assertion failed"], "patched_success": ["ok"],
                }), encoding="utf-8")
                return True, ""

        def producer(j, work, ctx):
            client = Client()
            client.workdir = work
            return repair.opencode_patch_producer(j, work, ctx, client=client, model="test")

        def verify(cmd, work, env):
            contents = (work / path).read_text(encoding="utf-8")
            assert new_tests in contents
            observed.append(contents)
            return (1, "regression: assertion failed", "") if "{ 0 }" in contents else (0, "regression ... ok", "")

        monkeypatch.setattr(repair, "_run_cmd", verify)
        out = tmp_path / "out"
        result = run_repair(job(sha), repo, out, patch_producer=producer)
        assert result.outcome == "succeeded"
        assert ["{ 0 }" in contents for contents in observed] == [True, False]
        assert "fn regression" in (out / "change.patch").read_text()
        assert len((out / "verification-steps.jsonl").read_text().splitlines()) == 2

    def test_full_chain_reproduced_and_fixed(self, tmp_path):
        r, sha = make_repo(tmp_path)
        producer = lambda job, work, ctx: (GOOD_PATCH, ["fake producer note"])
        res = run_repair(job(sha), r, tmp_path / "out",
                         test_cmd=[sys.executable, "-m", "pytest", "test_div.py", "-q"],
                         patch_producer=producer)
        assert res.reproduced_on_base is True
        assert res.fixed_on_patch is True
        assert res.regression_tests_passed is True
        assert res.outcome == "succeeded"
        assert (tmp_path / "out" / "change.patch").is_file()

    def test_base_passing_refuses_fix(self, tmp_path):
        """base 上测试本来就通过 → 拒绝"修"一个不存在的问题。

        验证现在严格按 base_sha 检出（2026-10-30），所以要把"已修好的代码"
        提交进 base，而不是只改工作区。
        """
        import subprocess
        r, sha = make_repo(tmp_path)
        (r / "calc.py").write_text("def div(a, b):\n    return a / b\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=r, check=True, capture_output=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                        "commit", "-qm", "already fixed"], cwd=r, check=True,
                       capture_output=True)
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=r, check=True,
                             capture_output=True, text=True).stdout.strip()
        res = run_repair(job(sha), r, tmp_path / "out",
                         test_cmd=[sys.executable, "-m", "pytest", "test_div.py", "-q"],
                         patch_producer=lambda j, w, c: {"patch": GOOD_PATCH, "notes": []})
        assert res.outcome == "blocked" and "base" in res.unverified_items[0]

    def test_wrong_patch_fails_not_fixed(self, tmp_path):
        r, sha = make_repo(tmp_path)
        bad = GOOD_PATCH.replace("return a / b", "return a * b")
        res = run_repair(job(sha), r, tmp_path / "out",
                         test_cmd=[sys.executable, "-m", "pytest", "test_div.py", "-q"],
                         patch_producer=lambda j, w, c: {"patch": bad, "notes": []})
        assert res.reproduced_on_base is True
        assert res.fixed_on_patch is False
        assert res.outcome == "failed"

    def test_missing_test_command_is_failed(self, tmp_path):
        """命令跑不起来时必须如实失败，不能声称修复。

        用 producer 注入补丁，避免依赖外部 opencode 服务。
        """
        r, sha = make_repo(tmp_path)
        res = run_repair(job(sha), r, tmp_path / "out",
                         test_cmd=["definitely-not-a-real-test-command"],
                         patch_producer=lambda j, w, c: {"patch": GOOD_PATCH,
                                                         "notes": []})
        assert res.outcome == "failed"
        assert res.fixed_on_patch is False
        assert res.unverified_items

    def test_patch_apply_failure(self, tmp_path):
        r, sha = make_repo(tmp_path)
        garbage = "not a valid diff"
        res = run_repair(job(sha), r, tmp_path / "out",
                         test_cmd=[sys.executable, "-m", "pytest", "test_div.py", "-q"],
                         patch_producer=lambda j, w, c: {"patch": garbage, "notes": []})
        # 不是 diff → check_patch_content 不报错，git apply 失败 → failed
        assert res.outcome == "failed" and "apply" in res.unverified_items[0]

    def test_no_test_cmd_blocked(self, tmp_path):
        """没有测试命令就无法验证——agent 也没报命令时直接失败。"""
        r, sha = make_repo(tmp_path)
        res = run_repair(job(sha), r, tmp_path / "out", test_cmd=[],
                         patch_producer=lambda j, w, c: {"patch": GOOD_PATCH,
                                                         "notes": []})
        assert res.outcome in ("blocked", "failed")
        assert not res.fixed_on_patch


class TestPatchContentSafety:
    @pytest.mark.parametrize("bad", [
        "--- a/C:/evil.py\n+++ b/C:/evil.py\n",
        "--- a/../../etc/passwd\n+++ b/../../etc/passwd\n",
        "--- a//abs/path\n+++ b//abs/path\n",
    ])
    def test_unsafe_patch_rejected(self, bad):
        assert check_patch_content(bad)

    def test_safe_patch_ok(self):
        assert not check_patch_content(GOOD_PATCH)


class TestDomainB:
    def test_opencode_json_written(self, tmp_path):
        from scout_v2.workers.repair import enforce_domain_b
        note = enforce_domain_b(tmp_path)
        cfg = json.loads((tmp_path / "opencode.json").read_text(encoding="utf-8"))
        # 2026-10-28：bash 必须放开，agent 要自己发现并运行仓库的测试框架
        assert cfg["permission"]["bash"] == "allow"
        assert cfg["permission"]["edit"] == "allow"
        assert cfg["permission"]["webfetch"] == "deny"
        assert "opencode.json" in note


class FakeClient:
    """替身：执行 mutate() 并记录收到的 prompt。"""

    def __init__(self, mutate):
        self.mutate = mutate
        self.prompts: list[str] = []

    def run(self, model: str, prompt: str) -> tuple[bool, str]:
        self.prompts.append(prompt)
        self.mutate()
        return True, "fixed div"


class TestPatchProducer:
    def test_prompt_via_task_file_not_multiline_argv(self, tmp_path):
        """Windows .cmd shim 实测：多行 argv 会挂起。prompt 必须经任务文件传递。"""
        from scout_v2.workers import repair as rp
        from scout_v2.workers.repair import opencode_patch_producer
        r, sha = make_repo(tmp_path)
        seen = {}

        class CapClient:
            def __init__(self, **kw):
                pass

            def run(self, model, prompt):
                seen["model"] = model
                seen["prompt"] = prompt
                seen["task_file"] = (r / rp.TASK_FILE).read_text(encoding="utf-8")
                (r / "calc.py").write_text("def div(a, b):\n    return a / b\n",
                                           encoding="utf-8")
                return True, "root cause: floor division"

        opencode_patch_producer(job(sha), r, "CONTEXT-LINE-ONE\nCONTEXT-LINE-TWO",
                                client=CapClient(), model="m")
        assert seen["prompt"] == rp.RUN_TASK_MSG            # argv 只有单行指令
        assert "CONTEXT-LINE-ONE" in seen["task_file"]      # 完整 prompt 在文件里
        assert "CONTEXT-LINE-TWO" in seen["task_file"]
        # 2026-10-28：agent 必须自己跑测试（旧 prompt 里的 "Do NOT run tests" 已移除）
        assert "run" in seen["task_file"].lower()
        assert "regression test" in seen["task_file"].lower()
        assert not (r / rp.TASK_FILE).exists()              # 任务文件不留在补丁里

    def test_new_files_included_and_config_not_in_patch(self, tmp_path):
        from scout_v2.workers.repair import opencode_patch_producer

        def mutate():
            (r / "fixer.py").write_text("# brand new module\n", encoding="utf-8")
            (r / "calc.py").write_text("def div(a, b):\n    return a / b\n", encoding="utf-8")

        r, sha = make_repo(tmp_path)
        res_p = opencode_patch_producer(job(sha), r, "ctx",
                                           client=FakeClient(mutate), model="m")
        patch = res_p["patch"]
        assert "fixer.py" in patch and "calc.py" in patch
        assert "opencode.json" not in patch
        assert not (r / "opencode.json").exists()

    def test_existing_opencode_json_restored(self, tmp_path):
        import subprocess
        from scout_v2.workers.repair import opencode_patch_producer
        r, _sha = make_repo(tmp_path)
        original = '{"theme":"dark"}\n'
        (r / "opencode.json").write_text(original, encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=r, check=True, capture_output=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                        "commit", "-qm", "add config"], cwd=r, check=True,
                       capture_output=True)
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=r, check=True,
                             capture_output=True, text=True).stdout.strip()
        c = FakeClient(lambda: (r / "calc.py").write_text(
            "def div(a, b):\n    return a / b\n", encoding="utf-8"))
        res_p = opencode_patch_producer(job(sha), r, "ctx", client=c, model="m")
        assert (r / "opencode.json").read_text(encoding="utf-8") == original
        assert "opencode.json" not in res_p["patch"]


class TestVerifyOnCleanBase:
    def test_producer_edits_worktree_do_not_break_apply(self, tmp_path):
        """真实 producer 会把改动留在 work 树里；验证前必须 reset 到 base 再 apply。"""
        import subprocess
        r, sha = make_repo(tmp_path)

        def producer(_job, work, _ctx):
            (work / "calc.py").write_text("def div(a, b):\n    return a / b\n",
                                          encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=work, check=True,
                           capture_output=True)
            diff = subprocess.run(["git", "diff", "--cached"], cwd=work, check=True,
                                  capture_output=True, text=True).stdout
            return diff, []

        res = run_repair(job(sha), r, tmp_path / "out",
                         test_cmd=[sys.executable, "-m", "pytest", "test_div.py", "-q"],
                         patch_producer=producer)
        assert res.outcome == "succeeded"
        assert res.reproduced_on_base is True and res.fixed_on_patch is True



class TestModelPlumbing:
    """2026-10-28 实测：worker 镜像里没有 configs/，resolve_model 在沙箱内必然失败。

    模型名必须由控制器解析后随 job 下发，worker 不应依赖配置文件。
    """

    def test_job_model_takes_precedence_over_config_lookup(self, tmp_path, monkeypatch):
        from scout_v2.workers import repair as rp
        r, sha = make_repo(tmp_path)

        def _boom():
            raise AssertionError("resolve_model must not be called when job.model is set")

        monkeypatch.setattr(rp, "resolve_model", _boom)
        seen: list[str] = []

        class Rec:
            """在 work 目录里改代码并记录模型名（FakeClient 硬编码打补丁到固定路径，
            这里必须落到 run_repair 传进来的 work 克隆上）。"""
            def __init__(self, work: Path):
                self.work = work

            def run(self, model, prompt):
                seen.append(model)
                (self.work / "calc.py").write_text(
                    "def div(a, b):\n    return a / b\n", encoding="utf-8")
                return True, "fixed div"

        j = job(sha)
        j.model = "opencode-go/deepseek-v4.1-flash"
        res = run_repair(j, r, tmp_path / "out",
                         test_cmd=[sys.executable, "-m", "pytest", "test_div.py", "-q"],
                         patch_producer=lambda jj, work, ctx: rp.opencode_patch_producer(
                             jj, work, ctx, client=Rec(work)))
        assert res.outcome == "succeeded"
        assert seen == ["opencode-go/deepseek-v4.1-flash"]

    def test_schema_carries_model(self):
        j = Job(mode="repair", repo="a/b", base_sha="0" * 40,
                idempotency_key="k", model="opencode-go/deepseek-v4.1-flash")
        assert j.to_dict()["model"] == "opencode-go/deepseek-v4.1-flash"
        assert Job.from_dict(j.to_dict()).model == "opencode-go/deepseek-v4.1-flash"


class TestWorkerImageContract:
    """worker 镜像不得依赖控制器的配置目录（构建上下文里没有 configs/）。"""

    def test_dockerfile_does_not_copy_configs(self):
        df = (Path(__file__).resolve().parents[1] / "docker" / "Dockerfile.worker").read_text(
            encoding="utf-8")
        copy_lines = [l for l in df.splitlines() if l.strip().upper().startswith("COPY ")]
        for line in copy_lines:
            assert "configs" not in line, f"worker image must not bundle configs: {line}"

    def test_dockerfile_pyyaml_present(self):
        """repair 的 resolve_model 兜底路径 import yaml；镜像缺 pyyaml 会 ModuleNotFoundError。"""
        df = (Path(__file__).resolve().parents[1] / "docker" / "Dockerfile.worker").read_text(
            encoding="utf-8")
        assert "pyyaml" in df.lower()


class TestEnvironmentNotMistakenForBug:
    """2026-10-28 实录（GitHub runner）：pytest 不可导入时 base 与 patched 两次
    运行都是 rc=1，前者被当成 reproduced_on_base=true —— 假阳性。

    带着这种"证据"去开 PR，等于提交一个什么都没验证过的改动。环境坏必须与
    "bug 复现"严格区分。
    """

    def test_broken_env_markers_are_recognised(self):
        from scout_v2.workers.repair import _test_env_broken
        assert _test_env_broken("", "/usr/bin/python3: No module named pytest\n")
        assert _test_env_broken("", "bash: pytest: command not found")
        assert _test_env_broken("", "Error: Cannot find module 'vitest'")
        assert _test_env_broken("", "'pytest' is not recognized as an internal or external command")
        # 真实断言失败不能被误判成环境问题
        assert _test_env_broken(
            "FAILED test_div.py::test_div - assert 3.0 == 3.5", "") is None
        assert _test_env_broken("", "E   AssertionError: assert 3 == 3.5") is None

    def test_missing_test_runner_is_not_reported_as_reproduced(self, tmp_path):
        """测试运行器不存在 → 环境问题，不是复现。

        用 producer 注入补丁，避免依赖外部 opencode 服务（否则这条测试会因为
        模型服务临时不可用而随机失败）。
        """
        r, sha = make_repo(tmp_path)
        res = run_repair(job(sha), r, tmp_path / "out",
                         test_cmd=["definitely-not-a-real-runner-xyz", "test_div.py"],
                         patch_producer=lambda j, w, c: {"patch": GOOD_PATCH,
                                                         "notes": []})
        assert res.reproduced_on_base is False
        assert res.fixed_on_patch is False
        assert res.outcome == "failed"


class TestRegressionFilesDelivered:
    """The verified regression must accompany the delivered fix."""

    def test_arbitrary_test_path_is_delivered(self, tmp_path):
        import subprocess
        r, sha = make_repo(tmp_path)

        def producer(j, work, ctx, **_kw):
            # 模拟 opencode：改源码 + 保留我们预置的测试文件
            (work / "calc.py").write_text("def div(a, b):\n    return a / b\n",
                                          encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=work, check=True,
                           capture_output=True)
            diff = subprocess.run(["git", "diff", "--cached"], cwd=work, check=True,
                                  capture_output=True, text=True).stdout
            return diff, []

        j = job(sha)
        res = run_repair(j, r, tmp_path / "out",
                         test_cmd=[sys.executable, "-m", "pytest", "test_custom_name.py", "-q"],
                         patch_producer=producer,
                         extra_test_files={"test_custom_name.py":
                                           "from calc import div\n\ndef test_x():\n"
                                           "    assert div(7, 2) == 3.5\n"})
        assert res.outcome == "succeeded"
        patch = (tmp_path / "out" / "change.patch").read_text(encoding="utf-8")
        assert "calc.py" in patch
        assert "test_custom_name.py" in patch


class TestReproductionMustBeReal:
    """2026-10-28 第二个假阳性：base 阶段测试文件还没落盘，pytest 以 usage error(4)
    退出，rc!=0 被当成"复现成功"。结果任何"补丁后测试能过"的改动都拿到伪造证据。

    真正的标准是：测试**在 base 上确实跑了并且失败**（断言失败），而不是
    pytest 报"文件不存在 / 没收集到测试 / 参数不认识"。
    """

    def test_seeded_test_runs_on_base_and_genuinely_fails(self, tmp_path):
        """base 上必须真的执行了这个用例并断言失败，而不是文件缺失。"""
        r, sha = make_repo(tmp_path)
        # 模拟 agent：自己报告测试命令，并修好 calc.py
        cmd = [sys.executable, "-m", "pytest", "test_div.py", "-q"]

        def producer(_j, work, _c):
            fixed = "def div(a, b):\n    return a / b\n"
            (work / "calc.py").write_text(fixed, encoding="utf-8")
            import subprocess as _sp
            _sp.run(["git", "add", "-A"], cwd=work, check=True, capture_output=True)
            diff = _sp.run(["git", "diff", "--cached"], cwd=work, check=True,
                           capture_output=True, text=True).stdout
            return {"patch": diff, "test_cmd": cmd, "notes": []}

        res = run_repair(job(sha), r, tmp_path / "out", patch_producer=producer)
        assert res.outcome == "succeeded"
        assert res.reproduced_on_base is True
        assert res.fixed_on_patch is True

    def test_missing_test_file_is_not_a_reproduction(self, tmp_path):
        """测试文件不存在 → 环境/调用问题，绝不能记成复现。"""
        r, sha = make_repo(tmp_path)
        res = run_repair(job(sha), r, tmp_path / "out",
                         test_cmd=[sys.executable, "-m", "pytest",
                                   "no_such_test_file.py", "-q"],
                         patch_producer=lambda j, w, c: {"patch": GOOD_PATCH,
                                                         "notes": []})
        assert res.reproduced_on_base is False
        assert res.outcome != "succeeded"

    @pytest.mark.parametrize(("rc", "output", "expected"), [
        (1, "clickhouse was not found in PATH", "missing_dependency"),
        (1, "ModuleNotFoundError: No module named 'clickhouse_driver'", "missing_dependency"),
        (127, "[Errno 2] No such file or directory: 'PATH=$PWD/build/programs:$PATH'", "command_error"),
        (1, "./multidb_fd_test.go:440:69: undefined: fdOutcomeToken\nFAIL redis [build failed]", "compile_failed"),
        (1, "test_div FAILED: unrelated assertion", "base_failure_unconfirmed"),
        (0, "test_io.test_textio passed\nTotal tests: run=2 (filtered)", "base_test_passed"),
    ])
    def test_historical_base_results_do_not_release_patch(self, tmp_path, monkeypatch, rc, output, expected):
        from scout_v2.workers import repair

        r, sha = make_repo(tmp_path)
        commands = []

        def run(cmd, cwd, env):
            commands.append(cmd)
            return rc, output, ""

        monkeypatch.setattr(repair, "_run_cmd", run)
        out = tmp_path / "out"
        result = run_repair(job(sha), r, out, patch_producer=lambda *_: {
            "patch": GOOD_PATCH, "test_cmd": ["pytest", "-v", "test_div.py"],
            "test_files": {"test_div.py": (r / "test_div.py").read_text()},
            "verification_plan": replay_plan(),
        })
        assert not result.reproduced_on_base and not result.fixed_on_patch
        assert result.unverified_items[0].startswith(expected)
        assert len(commands) == 1
        assert json.loads((out / "test-report.json").read_text())["failure_kind"] == expected
        assert output in (out / "verification.log").read_text()

    def test_agent_setup_runs_in_each_fresh_tree(self, tmp_path, monkeypatch):
        from scout_v2.workers import repair

        r, sha = make_repo(tmp_path)
        setup = {"kind": "build", "command": 'python -c "open(\'ready\', \'w\').close()"'}
        test = (r / "test_div.py").read_text().replace(
            "    assert", "    assert __import__('pathlib').Path('ready').is_file()\n    assert")
        commands = []
        real_run = repair._run_cmd

        def run(cmd, cwd, env):
            commands.append((cmd, cwd.name))
            return real_run(cmd, cwd, env)

        monkeypatch.setattr(repair, "_run_cmd", run)
        result = run_repair(job(sha), r, tmp_path / "out", patch_producer=lambda *_: {
            "patch": GOOD_PATCH, "test_cmd": [sys.executable, "-m", "pytest", "-v", "test_div.py::test_div"],
            "test_files": {"test_div.py": test},
            "verification_plan": replay_plan(setup_steps=[setup]),
        })
        assert result.outcome == "succeeded"
        assert [cwd for _, cwd in commands] == ["verify-base", "verify-base", "verify-patched", "verify-patched"]
        assert commands[1][0] == commands[3][0]

    def test_patched_success_must_be_from_same_case(self, tmp_path, monkeypatch):
        from scout_v2.workers import repair

        r, sha = make_repo(tmp_path)
        outcomes = iter([(1, "test_div FAILED: assert 3 == 3.5", ""), (0, "test_other PASSED", "")])
        monkeypatch.setattr(repair, "_run_cmd", lambda *_: next(outcomes))
        result = run_repair(job(sha), r, tmp_path / "out", patch_producer=lambda *_: {
            "patch": GOOD_PATCH, "test_cmd": ["pytest", "-v", "test_div.py"],
            "test_files": {"test_div.py": (r / "test_div.py").read_text()},
            "verification_plan": replay_plan(),
        })
        assert result.reproduced_on_base and not result.fixed_on_patch
        assert result.unverified_items[0].startswith("patched_test_unconfirmed")

    @pytest.mark.parametrize(("failed", "passed"), [
        ("test_div (test_calc.TestCalc.test_div) ... FAIL", "test_div (test_calc.TestCalc.test_div) ... ok"),
        ("test test_div ... FAILED", "test test_div ... ok"),
        ("test_div FAILED", "test_div PASSED"),
    ])
    def test_real_runner_status_change_preserves_case_identity(self, tmp_path, monkeypatch, failed, passed):
        from scout_v2.workers import repair

        r, sha = make_repo(tmp_path)
        outcomes = iter([(1, failed + "\nAssertionError: 3 != 3.5", ""), (0, passed, "")])
        monkeypatch.setattr(repair, "_run_cmd", lambda *_: next(outcomes))
        result = run_repair(job(sha), r, tmp_path / "out", patch_producer=lambda *_: {
            "patch": GOOD_PATCH, "test_cmd": ["python", "-m", "unittest", "-v", "test_div"],
            "test_files": {"test_div.py": (r / "test_div.py").read_text()},
            "verification_plan": {"setup_steps": [], "base_failure": [failed, "AssertionError: 3 != 3.5"],
                                  "patched_success": [passed]},
        })
        assert result.outcome == "succeeded" and result.reproduced_on_base and result.fixed_on_patch

    def test_production_agent_cannot_fall_back_to_a_bare_command(self, tmp_path, monkeypatch):
        from scout_v2.workers import repair

        r, sha = make_repo(tmp_path)
        monkeypatch.setattr(repair, "opencode_patch_producer", lambda *args, **kwargs: {
            "patch": GOOD_PATCH, "test_cmd": ["pytest", "-v", "test_div.py"],
        })
        result = run_repair(job(sha), r, tmp_path / "out")
        assert result.outcome == "blocked"
        assert result.unverified_items[0].startswith("verification_plan_invalid")

    def test_wasmtime_assertion_replay_still_passes(self, tmp_path, monkeypatch):
        from scout_v2.workers import repair

        r, sha = make_repo(tmp_path)
        case = "copying_collector_grow_heuristic_uses_active_semi_space"
        outcomes = iter([(101, f"test {case} ... FAILED\nassertion `left == right` failed", ""),
                         (0, f"test {case} ... ok", "")])
        monkeypatch.setattr(repair, "_run_cmd", lambda *_: next(outcomes))
        result = run_repair(job(sha), r, tmp_path / "out", patch_producer=lambda *_: {
            "patch": GOOD_PATCH, "test_cmd": ["cargo", "test", case],
            "test_files": {"test_div.py": (r / "test_div.py").read_text()},
            "verification_plan": {"setup_steps": [], "base_failure": [case, "assertion `left == right` failed"],
                                  "patched_success": ["... ok"]},
        })
        assert result.outcome == "succeeded" and result.reproduced_on_base and result.fixed_on_patch

    def test_rclone_quicktest_preserves_passed_regression_and_check_output(self, tmp_path, monkeypatch):
        from scout_v2.workers import repair

        r, sha = make_repo(tmp_path)
        j = job(sha)
        j.repo = "rclone/rclone"
        monkeypatch.setenv("OPENCODE_API_KEY", "test-only-opencode-secret")
        outcomes = iter([(1, "test_div FAILED: assert 3 == 3.5", ""), (0, "test_div PASSED", ""),
                         (0, "go build passed", ""),
                         (2, "quicktest failing package", "make: Error 2 test-only-opencode-secret")])
        monkeypatch.setattr(repair, "_run_cmd", lambda *_: next(outcomes))
        out = tmp_path / "out"
        result = run_repair(j, r, out, patch_producer=lambda *_: {
            "patch": GOOD_PATCH, "test_cmd": ["go", "test"],
            "test_files": {"test_div.py": (r / "test_div.py").read_text()},
            "verification_plan": replay_plan(),
        })
        assert result.outcome == "failed" and result.reproduced_on_base and result.fixed_on_patch
        assert result.regression_tests_passed
        assert result.unverified_items[0].startswith("additional_check_failed")
        report = json.loads((out / "test-report.json").read_text())
        assert report["additional_check_output"]["make quicktest"]["stderr_tail"] == "make: Error 2 [REDACTED]"
        assert "quicktest failing package" in (out / "verification.log").read_text()
        assert "test-only-opencode-secret" not in (out / "verification.log").read_text()

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX environment assignment")
    def test_clickhouse_path_assignment_is_executed_by_shell(self, tmp_path):
        import os
        from scout_v2.workers import repair

        cmd = 'PATH="$PWD/build/programs:$PATH" printenv PATH'
        rc, stdout, stderr = repair._run_cmd(repair._split_cmd(cmd), tmp_path, os.environ)
        assert (rc, stderr) == (0, "")
        assert stdout.startswith(f"{tmp_path}/build/programs:")

    def test_environment_assignment_does_not_turn_grep_into_regression_test(self):
        from scout_v2.workers import repair

        cmd = repair._split_cmd("PATH=$PWD/bin:$PATH grep -q token calc.py")
        assert repair._implausible_verification(cmd, GOOD_PATCH, {"test_div.py": ""})

    def test_seeded_file_is_written_for_base_then_removed(self, tmp_path):
        """AI 测试文件要能被 base 阶段看到，但跑完必须清理，不污染工作区。"""
        from scout_v2.workers.repair import (_seed_test_files,
                                             _unseed_test_files)
        r, sha = make_repo(tmp_path)
        assert not (r / "test_generated.py").exists()
        created = _seed_test_files(r, {"test_generated.py": "def test_x():\n    pass\n"})
        assert (r / "test_generated.py").exists()
        assert created == [(r / "test_generated.py", None)]   # None = 我们新建的
        _unseed_test_files(r, created)
        assert not (r / "test_generated.py").exists()

    def test_existing_file_is_overwritten_then_restored(self, tmp_path):
        """agent 常在已有的测试文件里追加用例，必须覆盖后再还原。

        2026-10-30 实测：不覆盖会让 base 阶段 `pytest path::new_case` 报
        "no match" —— 那个新用例在 base 版本里本来就不存在，看起来像环境坏了。
        """
        from scout_v2.workers.repair import (_seed_test_files,
                                             _unseed_test_files)
        r, sha = make_repo(tmp_path)
        original = (r / "test_div.py").read_text(encoding="utf-8")
        created = _seed_test_files(r, {"test_div.py": "SHOULD WIN\n"})
        assert (r / "test_div.py").read_text(encoding="utf-8") == "SHOULD WIN\n"
        _unseed_test_files(r, created)
        assert (r / "test_div.py").read_text(encoding="utf-8") == original


def monkeypatch_(module, name, value):
    """极简 setattr 补丁（测试内联用，避免依赖 pytest fixture 传递）。"""
    old = getattr(module, name)
    setattr(module, name, value)

    def undo():
        setattr(module, name, old)
    return undo


