"""Agent 完整交付、补丁安全、模型选择与运行环境测试。"""
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
