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
                         patch_producer=lambda j, w, c: (GOOD_PATCH, []))
        assert res.outcome == "blocked" and "reproduce" in res.unverified_items[0]

    def test_wrong_patch_fails_not_fixed(self, tmp_path):
        r, sha = make_repo(tmp_path)
        bad = GOOD_PATCH.replace("return a / b", "return a * b")
        res = run_repair(job(sha), r, tmp_path / "out",
                         test_cmd=[sys.executable, "-m", "pytest", "test_div.py", "-q"],
                         patch_producer=lambda j, w, c: (bad, []))
        assert res.reproduced_on_base is True
        assert res.fixed_on_patch is False
        assert res.outcome == "failed"

    def test_missing_test_command_is_failed(self, tmp_path):
        r, sha = make_repo(tmp_path)
        res = run_repair(job(sha), r, tmp_path / "out",
                         test_cmd=["definitely-not-a-real-test-command"])
        assert res.outcome == "failed"
        assert res.reproduced_on_base is False
        assert "not runnable" in res.unverified_items[0]

    def test_patch_apply_failure(self, tmp_path):
        r, sha = make_repo(tmp_path)
        garbage = "not a valid diff"
        res = run_repair(job(sha), r, tmp_path / "out",
                         test_cmd=[sys.executable, "-m", "pytest", "test_div.py", "-q"],
                         patch_producer=lambda j, w, c: (garbage, []))
        # 不是 diff → check_patch_content 不报错，git apply 失败 → failed
        assert res.outcome == "failed" and "apply" in res.unverified_items[0]

    def test_no_test_cmd_blocked(self, tmp_path):
        r, sha = make_repo(tmp_path)
        res = run_repair(job(sha), r, tmp_path / "out", test_cmd=[])
        assert res.outcome == "blocked"


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
        assert cfg["permission"]["bash"] == "deny"
        assert cfg["permission"]["edit"] == "allow"
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
        assert "Do NOT run tests" in seen["task_file"]
        assert not (r / rp.TASK_FILE).exists()              # 任务文件不留在补丁里

    def test_new_files_included_and_config_not_in_patch(self, tmp_path):
        from scout_v2.workers.repair import opencode_patch_producer

        def mutate():
            (r / "fixer.py").write_text("# brand new module\n", encoding="utf-8")
            (r / "calc.py").write_text("def div(a, b):\n    return a / b\n", encoding="utf-8")

        r, sha = make_repo(tmp_path)
        patch, _ = opencode_patch_producer(job(sha), r, "ctx",
                                           client=FakeClient(mutate), model="m")
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
        patch, _ = opencode_patch_producer(job(sha), r, "ctx", client=c, model="m")
        assert (r / "opencode.json").read_text(encoding="utf-8") == original
        assert "opencode.json" not in patch


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
