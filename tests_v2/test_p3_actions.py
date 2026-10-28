"""P3 测试：Actions 后端（mock gh）+ 收集校验 + broad_scan 拒绝路由。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scout_v2.adapters import actions_backend as ab  # noqa: E402
from scout_v2.schemas import Job  # noqa: E402


def job(mode="repair", sha="b" * 40) -> Job:
    return Job(mode=mode, repo="a/b", base_sha=sha,
               idempotency_key=f"repair:a/b:{sha}:fp",
               execution_scope="project_contribution")


class FakeGH:
    """按调用序返回脚本化响应。"""
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[list[str]] = []

    def __call__(self, args):
        self.calls.append(args)
        return self.responses.pop(0) if self.responses else (1, "script exhausted")


class TestEligibility:
    def test_broad_scan_always_ineligible(self):
        b = ab.ActionsBackend(enabled=True, repo="me/t", workflow_exists=True)
        j = job("repair"); j2 = j.to_dict(); j2["execution_scope"] = "broad_scan"
        assert not b.eligible(j2)

    def test_scan_reproduce_eligible_on_contribution(self):
        """能力对称裁定 2026-10-25：scan/reproduce 在 project_contribution 范围内
        与 repair/review/ci 同权；broad_scan 仍恒拒。"""
        b = ab.ActionsBackend(enabled=True, repo="me/t", workflow_exists=True)
        assert b.eligible(job("scan").to_dict())
        assert b.eligible(job("reproduce").to_dict())

    def test_disabled_or_no_workflow_ineligible(self):
        j = job().to_dict()
        assert not ab.ActionsBackend(enabled=False, repo="me/t", workflow_exists=True).eligible(j)
        assert not ab.ActionsBackend(enabled=True, repo="me/t", workflow_exists=False).eligible(j)

    def test_contribution_ok(self):
        b = ab.ActionsBackend(enabled=True, repo="me/t", workflow_exists=True)
        assert b.eligible(job().to_dict())


class TestSubmit:
    def test_dispatch_and_find_run(self):
        gh = FakeGH([(0, ""), (0, json.dumps({"workflow_runs": [{"id": 12345, "event": "workflow_dispatch"}]}))])
        b = ab.ActionsBackend(enabled=True, repo="me/t", workflow_exists=True, gh_runner=gh)
        run_id = b.submit(job().to_dict())
        assert run_id == "12345"
        assert gh.calls[0][2].endswith("/dispatches")

    def test_dispatch_failure_raises(self):
        gh = FakeGH([(1, "404 no workflow")])
        b = ab.ActionsBackend(enabled=True, repo="me/t", workflow_exists=True, gh_runner=gh)
        with pytest.raises(RuntimeError, match="dispatch failed"):
            b.submit(job().to_dict())

    def test_run_not_found_raises(self):
        gh = FakeGH([(0, "")] + [(0, json.dumps({"workflow_runs": []}))] * 8)
        b = ab.ActionsBackend(enabled=True, repo="me/t", workflow_exists=True, gh_runner=gh)
        with pytest.raises(RuntimeError, match="not found"):
            b.submit(job().to_dict())


class TestPollCollect:
    def test_poll_states(self):
        gh = FakeGH([
            (0, json.dumps({"status": "in_progress"})),
            (0, json.dumps({"status": "completed", "conclusion": "success"})),
        ])
        b = ab.ActionsBackend(enabled=True, repo="me/t", gh_runner=gh)
        assert b.poll("1")["state"] == "running"
        assert b.poll("1")["state"] == "succeeded"

    def test_collect_reads_artifact(self, tmp_path):
        # 先准备本地 artifact 目录，模拟 gh run download 的产物
        run_id = "777"
        dest = tmp_path / f"run-{run_id}"
        (dest / "result.json").parent.mkdir(parents=True)
        (dest / "result.json").write_text(json.dumps(
            {"job_id": "job-1", "outcome": "succeeded", "base_sha": "b" * 40}),
            encoding="utf-8")

        gh = FakeGH([(0, json.dumps({"artifacts": [{"id": 9}]}))])
        dl = FakeGH([(0, "")])
        b = ab.ActionsBackend(enabled=True, repo="me/t", gh_runner=gh,
                              download_runner=dl, artifact_dir=tmp_path)
        collected = b.collect(run_id)
        assert collected["job_id"] == "job-1"
        # 校验器：一致 → 无错
        j = job(sha="b" * 40); j.job_id = "job-1"
        assert ab.verify_collected(collected, j) == []
        # 校验器：SHA 不一致 → 报错
        j2 = job(sha="c" * 40); j2.job_id = "job-1"
        assert ab.verify_collected(collected, j2)

    def test_collect_no_artifacts_falls_back_to_log_envelope(self, tmp_path):
        gh = FakeGH([(0, json.dumps({"artifacts": []}))])
        log = "step1 ok\n##OSSCOUT_RESULT##" + json.dumps(
            {"result": {"job_id": "job-1", "outcome": "succeeded"}})
        b = ab.ActionsBackend(enabled=True, repo="me/t", gh_runner=gh, artifact_dir=tmp_path)
        b._logs_text = lambda rid: log
        out = b.collect("1")
        assert out["job_id"] == "job-1" and out["outcome"] == "succeeded"

    def test_collect_no_artifacts_no_envelope(self, tmp_path):
        gh = FakeGH([(0, json.dumps({"artifacts": []}))])
        b = ab.ActionsBackend(enabled=True, repo="me/t", gh_runner=gh, artifact_dir=tmp_path)
        b._logs_text = lambda rid: "plain logs, no marker"
        out = b.collect("1")
        assert out["outcome"] == "inconclusive"
        assert any("envelope" in u for u in out["unverified_items"])

    def test_collect_logs_unavailable(self, tmp_path):
        gh = FakeGH([(0, json.dumps({"artifacts": []}))])
        b = ab.ActionsBackend(enabled=True, repo="me/t", gh_runner=gh, artifact_dir=tmp_path)
        b._logs_text = lambda rid: None
        out = b.collect("1")
        assert out["outcome"] == "inconclusive"
        assert any("logs unavailable" in u for u in out["unverified_items"])

    def test_cancel(self):
        gh = FakeGH([(0, "")])
        b = ab.ActionsBackend(enabled=True, repo="me/t", gh_runner=gh)
        b.cancel("42")
        assert gh.calls[0][0] == "-X" and gh.calls[0][1] == "DELETE" \
            and "runs/42" in gh.calls[0][2]


class TestRunnerRepoContract:
    """2026-10-28 修正：repair 在**我们自己的**仓库的托管 runner 上跑。

    旧实现把 workflow_dispatch 发到目标仓库，前提是第三方仓库里存在我们的
    workflow 文件 —— 既不可行（不能往别人仓库塞 workflow）也违背最小侵入。
    这组测试锁住新契约，防止回退。
    """

    def _job(self):
        from scout_v2.schemas import Job
        return Job(mode="repair", repo="someone/third-party-repo",
                   base_sha="a" * 40, idempotency_key="k")

    def test_dispatch_targets_runner_repo_not_target_repo(self):
        calls: list[list[str]] = []

        def gh(args):
            calls.append(args)
            if "dispatches" in " ".join(args):
                return 0, ""
            return 0, '{"workflow_runs": [{"id": 42, "event": "workflow_dispatch"}]}'

        b = ab.ActionsBackend(enabled=True, runner_repo="me/runner", workflow_exists=True,
                              gh_runner=gh)
        rid = b.submit(self._job().to_dict())
        assert rid == "42"
        dispatch = next(c for c in calls if any("dispatches" in a for a in c))
        joined = " ".join(dispatch)
        assert "repos/me/runner/" in joined
        assert "someone/third-party-repo" not in joined

    def test_target_repo_travels_in_payload(self):
        seen: dict = {}

        def gh(args):
            joined = " ".join(args)
            if "dispatches" in joined:
                for i, a in enumerate(args):
                    if a.startswith("inputs[oss_job_b64]="):
                        import base64 as b64
                        seen.update(json.loads(
                            b64.b64decode(a.split("=", 1)[1]).decode("utf-8")))
                return 0, ""
            return 0, '{"workflow_runs": [{"id": 7, "event": "workflow_dispatch"}]}'

        b = ab.ActionsBackend(enabled=True, runner_repo="me/runner",
                              workflow_exists=True, gh_runner=gh)
        b.submit(self._job().to_dict())
        assert seen["repo"] == "someone/third-party-repo"
        assert seen["base_sha"] == "a" * 40

    def test_ineligible_without_target_or_sha(self):
        b = ab.ActionsBackend(enabled=True, runner_repo="me/runner", workflow_exists=True)
        assert not b.eligible({"mode": "repair", "repo": "", "base_sha": "a" * 40})
        assert not b.eligible({"mode": "repair", "repo": "a/b", "base_sha": ""})
        assert not b.eligible({"mode": "repair", "repo": "a/b", "base_sha": "a" * 40,
                               "execution_scope": "broad_scan"})

    def test_collect_returns_patch_and_report(self, tmp_path):
        run_dir = tmp_path / "run-9"
        run_dir.mkdir()
        (run_dir / "result.json").write_text(json.dumps(
            {"job_id": "j", "outcome": "succeeded", "base_sha": "a" * 40}),
            encoding="utf-8")
        (run_dir / "change.patch").write_text("--- a/x\n+++ b/x\n", encoding="utf-8")
        (run_dir / "test-report.json").write_text('{"ok": true}', encoding="utf-8")

        def gh(args):
            return 0, json.dumps({"artifacts": [{"name": "oss-result-j"}]})

        def dl(args):
            return 0, ""

        b = ab.ActionsBackend(enabled=True, runner_repo="me/runner", workflow_exists=True,
                              gh_runner=gh, download_runner=dl, artifact_dir=tmp_path)
        # 让下载"成功"地指向已备好的目录：直接调用解析层
        _p, data = b._load_result(run_dir)
        assert data is not None
        assert data["outcome"] == "succeeded"
        assert "change.patch" not in data and data["patch"].startswith("--- a/x")
        assert data["test_report"] == '{"ok": true}'


class TestPatchSurvivesWithoutArtifacts:
    """artifact 配额满时必须仍能拿到补丁。

    2026-10-28 实录：账号 artifact 配额耗尽，upload 整包失败。补丁本来还能从
    stdout 信封取回，但有两处把 patch 丢掉了，于是流程停在"patch=no"，
    检测到真 bug 也开不出 PR。
    """

    def test_log_fallback_keeps_patch_and_report(self, monkeypatch):
        import base64 as b64
        envelope = {
            "result": {"job_id": "j1", "outcome": "succeeded",
                       "reproduced_on_base": True, "fixed_on_patch": True,
                       "regression_tests_passed": True,
                       "base_sha": "a" * 40, "unverified_items": []},
            "patch": "--- a/calc.py\n+++ b/calc.py\n@@ -1 +1 @@\n-a // b\n+a / b\n",
            "test_report": {"base_returncode": 1, "patched_returncode": 0},
        }
        text = "##OSSCOUT_RESULT##" + json.dumps(envelope)

        b = ab.ActionsBackend(enabled=True, runner_repo="me/runner", workflow_exists=True,
                              gh_runner=lambda a: (1, ""),  # 强制走日志回退
                              download_runner=lambda a: (1, ""))
        monkeypatch.setattr(b, "_logs_text", lambda _rid: text)
        out = b.collect("123")
        assert out["patch"].startswith("--- a/calc.py")
        assert out["test_report"]["base_returncode"] == 1
        assert out["outcome"] == "succeeded"

    def test_pipeline_collect_unpacks_actions_shape(self):
        """Actions 返回摊平 dict；_collect 必须把 patch/test_report 提取出来。"""
        from pathlib import Path
        from scout_v2.workers import auto_pipeline as ap

        flat = {"job_id": "j1", "outcome": "succeeded", "base_sha": "a" * 40,
                "patch": "PATCHTEXT", "test_report": {"ok": True}}

        class FakeBackend:
            def poll(self, _rid):
                return {"state": "succeeded"}

            def collect(self, _rid):
                return flat

        got = ap._collect(Path("."), "github_actions", "1", {"backend_obj": FakeBackend()},
                          timeout_s=5)
        assert got["patch"] == "PATCHTEXT"
        assert got["test_report"] == {"ok": True}
        assert got["result"]["outcome"] == "succeeded"
        assert "patch" not in got["result"]


class TestPublishAuth:
    """控制器是唯一写者，push 必须带凭据。

    2026-10-28 实录：容器里 GH_TOKEN 有效，但 git 没配 credential helper，
    裸 `git push origin` 报 "could not read Username"，整条链停在最后一步。
    同时令牌绝不允许出现在日志或摘要里。
    """

    def test_push_url_carries_token_and_origin_is_scrubbed(self, tmp_path, monkeypatch):
        import subprocess as sp
        from scout_v2.workers import auto_pipeline as ap

        monkeypatch.setenv("GH_TOKEN", "ghp_SECRETTOKEN123")
        seen: list[list[str]] = []

        def fake_run(cmd, **kw):
            seen.append(list(cmd))
            class R:
                returncode = 0
                stdout = ""
                stderr = ""
            if cmd[:2] == ["git", "clone"]:
                # 模拟真实 clone：建出目录，后续步骤才能在 cwd 里跑
                target = cmd[-1]
                import os
                os.makedirs(target, exist_ok=True)
                sp.run(["git", "init", "-q", target], check=True, capture_output=True)
            return R()

        monkeypatch.setattr(ap.subprocess, "run", fake_run)
        patch = tmp_path / "change.patch"
        patch.write_text("--- a/x\n+++ b/x\n", encoding="utf-8")
        summary: dict = {}
        ap._publish_pr(tmp_path, {"repo": "me/t", "number": 8, "title": "fix"},
                       "a" * 40, patch, summary)

        clone_cmd = next(c for c in seen if c[:2] == ["git", "clone"])
        # clone 上游用干净 URL（公共仓库匿名即可），token 不进 clone 命令
        assert clone_cmd[-2] == "https://github.com/me/t.git"
        assert "ghp_SECRETTOKEN123" not in clone_cmd[-2]
        # token 只出现在 fork remote 的 URL 里（推送需要它）
        fork = next(c for c in seen if "remote" in c and "add" in c)
        assert "ghp_SECRETTOKEN123" in fork[-1]
        assert "xixi-box/t.git" in fork[-1]
        push_cmd = next(c for c in seen if "push" in c)
        # push 走 fork remote（名字），URL 里才带凭据
        assert "fork" in push_cmd
        assert "ghp_SECRETTOKEN123" not in " ".join(push_cmd)
        assert "ghp_SECRETTOKEN123" not in str(summary.get("abort", ""))

    def test_third_party_repo_pushes_to_fork_not_upstream(self):
        """对第三方仓库没有 push 权限，分支必须推自己的 fork。"""
        from scout_v2.workers.auto_pipeline import _fork_of
        assert _fork_of("anomalyco/opencode") == "xixi-box/opencode"
        assert _fork_of("microsoft/playwright") == "xixi-box/playwright"
        # 自己名下的仓库不重复加前缀
        assert _fork_of("xixi-box/oss-scout-v2-test") == "xixi-box/oss-scout-v2-test"

    def test_push_failure_never_leaks_token(self, tmp_path, monkeypatch):
        from scout_v2.workers import auto_pipeline as ap

        monkeypatch.setenv("GH_TOKEN", "ghp_SECRETTOKEN123")

        def fake_run(cmd, **kw):
            class R:
                returncode = 1
                stdout = ""
                stderr = "fatal: could not push to https://x-access-token:ghp_SECRETTOKEN123@github.com"
            return R()

        monkeypatch.setattr(ap.subprocess, "run", fake_run)
        patch = tmp_path / "change.patch"
        patch.write_text("--- a/x\n+++ b/x\n", encoding="utf-8")
        summary: dict = {}
        assert ap._publish_pr(tmp_path, {"repo": "me/t", "number": 8},
                              "a" * 40, patch, summary) is None
        assert "ghp_SECRETTOKEN123" not in summary["abort"]
        assert "***" in summary["abort"]


class TestNoTokenInTracebacks:
    """令牌绝不允许出现在任何异常路径里。

    2026-10-28 实录：clone 超时抛 subprocess.TimeoutExpired，其 cmd 字段带完整
    URL（含 ghp_ 令牌），traceback 直接把它打进终端与日志。只重写 stderr 不够，
    必须让超时走普通返回路径。
    """

    def test_clone_timeout_does_not_raise_or_leak(self, tmp_path, monkeypatch):
        import subprocess as sp
        from scout_v2.workers import auto_pipeline as ap

        monkeypatch.setenv("GH_TOKEN", "ghp_SECRETLEAK")
        calls = {"n": 0}

        def fake_run(cmd, **kw):
            calls["n"] += 1
            raise sp.TimeoutExpired(cmd, kw.get("timeout", 180))

        monkeypatch.setattr(ap.subprocess, "run", fake_run)
        res = ap._git_retry(["clone", "https://x-access-token:ghp_SECRETLEAK@github.com/x/y.git",
                             str(tmp_path)], timeout=1, attempts=2,
                            secret="ghp_SECRETLEAK")
        assert res.returncode != 0
        assert "ghp_SECRETLEAK" not in (res.stderr or "")
        assert calls["n"] == 2

    def test_publish_clone_timeout_aborts_without_leak(self, tmp_path, monkeypatch):
        import subprocess as sp
        from scout_v2.workers import auto_pipeline as ap

        monkeypatch.setenv("GH_TOKEN", "ghp_SECRETLEAK")

        def fake_run(cmd, **kw):
            raise sp.TimeoutExpired(cmd, kw.get("timeout", 180))

        monkeypatch.setattr(ap.subprocess, "run", fake_run)
        patch = tmp_path / "change.patch"
        patch.write_text("--- a/x\n+++ b/x\n", encoding="utf-8")
        summary: dict = {}
        assert ap._publish_pr(tmp_path, {"repo": "me/t", "number": 8},
                              "a" * 40, patch, summary) is None
        assert "ghp_SECRETLEAK" not in json.dumps(summary)
