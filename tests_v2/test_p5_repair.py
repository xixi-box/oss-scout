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

