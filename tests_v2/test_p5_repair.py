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


class TestRepairEvidenceChain:
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
        r, sha = make_repo(tmp_path)
        # 覆盖 calc.py 为正确实现 → base 测试通过 → 拒绝盲修
        (r / "calc.py").write_text("def div(a, b):\n    return a / b\n", encoding="utf-8")
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
        res = run_repair(job(sha), r, tmp_path / "out", test_cmd=[])
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
        r, sha = make_repo(tmp_path)
        res = run_repair(job(sha), r, tmp_path / "out",
                         test_cmd=["definitely-not-a-real-runner-xyz", "test_div.py"])
        assert res.reproduced_on_base is False
        assert res.fixed_on_patch is False
        assert res.outcome == "failed"


class TestAiTestFilesExcludedFromPatch:
    """AI 回归测试是验证手段，不是修复内容，绝不能进补丁。

    2026-10-28 实录：排除列表硬编码 test_ai_regression_issue.py，而实际路径是
    由 job.ai_test_files 决定的（这次是 test_ai_regression.py），于是测试文件
    连同修复一起进了 patch。
    """

    def test_arbitrary_test_path_is_excluded(self, tmp_path):
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
        assert "test_custom_name.py" not in patch


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

    def test_seeded_file_is_written_for_base_then_removed(self, tmp_path):
        """AI 测试文件要能被 base 阶段看到，但跑完必须清理，不污染工作区。"""
        from scout_v2.workers.repair import (_seed_test_files,
                                             _unseed_test_files)
        r, sha = make_repo(tmp_path)
        assert not (r / "test_generated.py").exists()
        created = _seed_test_files(r, {"test_generated.py": "def test_x():\n    pass\n"})
        assert (r / "test_generated.py").exists()
        assert created == [r / "test_generated.py"]
        _unseed_test_files(r, created)
        assert not (r / "test_generated.py").exists()

    def test_existing_file_is_never_overwritten_or_deleted(self, tmp_path):
        from scout_v2.workers.repair import (_seed_test_files,
                                             _unseed_test_files)
        r, sha = make_repo(tmp_path)
        original = (r / "test_div.py").read_text(encoding="utf-8")
        created = _seed_test_files(r, {"test_div.py": "SHOULD NOT WIN\n"})
        assert created == []
        assert (r / "test_div.py").read_text(encoding="utf-8") == original
        _unseed_test_files(r, created)
        assert (r / "test_div.py").exists()


def monkeypatch_(module, name, value):
    """极简 setattr 补丁（测试内联用，避免依赖 pytest fixture 传递）。"""
    old = getattr(module, name)
    setattr(module, name, value)

    def undo():
        setattr(module, name, old)
    return undo


class TestRemoteCommandPortability:
    """test_cmd 会跨机器下发，绝不能带控制器本机的绝对路径。

    2026-10-28 实录：cmd 用 sys.executable（/usr/local/bin/python，VPS 容器里的
    路径），发到 GitHub runner 上找不到这个文件，base 阶段直接 exit 127
    "command likely not runnable"，整条证据链判定失败。
    """

    def test_python_cmd_uses_path_name_not_absolute_interpreter(self):
        import sys as _sys
        from scout_v2.workers import auto_pipeline as ap
        import scout_v2.workers.issue_scorer as isc

        # 只替换最底层的单次请求：chat_with_fallback 本身就带降级链与 tenacity
        # 重试，整体替换 FakeClient 会撞上它内部依赖的 _Retryable。
        test_src = "\n".join([
            "from calc import div",
            "",
            "def test_x():",
            "    assert div(7, 2) == 3.5",
            "",
        ])
        payload = {"choices": [{"message": {"content": test_src}}]}

        undo_client = monkeypatch_(isc.GoClient, "_chat_once",
                                   lambda self, body: payload)
        undo_cfg = monkeypatch_(ap, "load_scorer_config",
                                lambda *_a, **_k: {"api_key": "k", "model": "m"})
        try:
            job = Job(mode="repair", repo="a/b", base_sha="a" * 40,
                      idempotency_key="k", context="ctx")
            cmd, rel, code = ap._ai_generate_test_remote(
                job, "python", "python -m pytest {path} -q", "tests",
                ["calc.py"], {"steps": []})
        finally:
            undo_client()
            undo_cfg()

        assert cmd is not None
        assert cmd[0] != _sys.executable
        assert cmd[:3] == ["python3", "-m", "pytest"]


