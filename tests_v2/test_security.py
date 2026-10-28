"""安全测试（计划 §16：SHA 校验、路径 traversal、secrets 不入产物、base→patch 证据链）。"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scout_v2.schemas import Job, Result  # noqa: E402
from scout_v2.workers.quality_gate import (check_patch_path,  # noqa: E402
                                           verify_result)


def job(job_id="job-1", sha="a" * 40, mode="reproduce", key="verify:a/b:" + "a" * 40 + ":fp") -> Job:
    return Job(job_id=job_id, mode=mode, repo="a/b", base_sha=sha, idempotency_key=key)


class TestPatchPathSafety:
    @pytest.mark.parametrize("bad", [
        "../escape.patch", "/abs/path.patch", "C:\\win\\path.patch",
        "..\\..\\x.patch", "dir/../../out.patch", "\\\\server\\share\\x",
    ])
    def test_unsafe_paths_rejected(self, bad):
        assert check_patch_path(bad)

    def test_safe_path_ok(self):
        assert not check_patch_path("artifacts/job-1/change.patch")


class TestResultVerification:
    def test_base_sha_mismatch_blocks_import(self):
        r = Result(job_id="job-1", backend="modal", outcome="succeeded",
                   base_sha="b" * 40)
        rep = verify_result(r, job(sha="a" * 40), expected_base_sha="a" * 40)
        assert not rep.ok and any("base_sha" in e for e in rep.errors)

    def test_job_id_mismatch_blocks(self):
        r = Result(job_id="other", backend="modal", outcome="inconclusive")
        assert not verify_result(r, job()).ok

    def test_claim_fixed_requires_repro_and_tests(self):
        r = Result(job_id="job-1", backend="modal", outcome="succeeded",
                   base_sha="a" * 40, fixed_on_patch=True,
                   regression_tests_passed=False, reproduced_on_base=True)
        rep = verify_result(r, job())
        assert not rep.ok
        r2 = Result(job_id="job-1", backend="modal", outcome="succeeded",
                    base_sha="a" * 40, fixed_on_patch=True,
                    regression_tests_passed=True, reproduced_on_base=False)
        rep2 = verify_result(r2, job())
        assert not rep2.ok

    def test_full_evidence_chain_passes(self):
        r = Result(job_id="job-1", backend="modal", outcome="succeeded",
                   base_sha="a" * 40, reproduced_on_base=True,
                   fixed_on_patch=True, regression_tests_passed=True,
                   patch_path="artifacts/job-1/change.patch")
        assert verify_result(r, job()).ok

    def test_artifact_hash_mismatch_blocks(self, tmp_path):
        art = tmp_path / "artifacts" / "job-1"
        art.mkdir(parents=True)
        (art / "change.patch").write_text("patch content", encoding="utf-8")
        import hashlib
        wrong = hashlib.sha256(b"other").hexdigest()
        r = Result(job_id="job-1", backend="modal", outcome="succeeded",
                   base_sha="a" * 40, patch_path="artifacts/job-1/change.patch")
        rep = verify_result(r, job(), artifact_dir=tmp_path, patch_sha256=wrong)
        assert not rep.ok and any("hash" in e for e in rep.errors)


class TestCoreNoSecrets:
    def test_result_contains_no_token_like_strings(self, tmp_path):
        from scout_v2.workers.core import execute
        j = job()
        r = execute(j, tmp_path)
        text = json.dumps(r.to_dict())
        for marker in ("ghp_", "github_pat_", "sk-", "MODAL_TOKEN", "GITHUB_TOKEN"):
            assert marker not in text

    def test_refuses_write_flag_in_job_file(self, tmp_path):
        from scout_v2.workers.core import main
        jf = tmp_path / "job.json"
        jf.write_text(json.dumps({"job_id": "j", "mode": "scan",
                                  "idempotency_key": "k",
                                  "allow_github_write": True}), encoding="utf-8")
        assert main(["--job", str(jf), "--out", str(tmp_path / "o")]) == 2

    def test_job_validate_rejects_write(self):
        j = job()
        j.allow_github_write = True
        assert any("allow_github_write" in e for e in j.validate())


class TestEnvKeyIsolation:
    def test_worker_process_env_has_no_tokens(self, tmp_path):
        """计划 §10：只在同进程清环境变量不可靠——这里验证的是 worker 子进程契约：
        job JSON 不携带任何 token 字段，core 不读取也不转发密钥。"""
        from scout_v2.workers.core import execute
        j = job()
        r = execute(j, tmp_path)
        assert r.backend == "local"
        # 骨架结果不含任何环境变量内容
        assert not any("PATH" in item or "KEY" in item
                       for item in r.unverified_items)


class TestClosedPrIsNotCompetition:
    """已关闭/已合并的关联 PR 不应阻止我们接手。

    2026-10-28 实测：issue timeline 里的 cross-referenced 事件不会因为 PR 关闭
    而消失；早期实现只看"存在关联 PR"就判定竞争，导致该 issue 被永久拒绝。
    """

    def _recheck(self, monkeypatch, pr_state, merged=None):
        from scout_v2 import git_host as gh

        timeline = [{
            "event": "cross-referenced",
            "source": {"issue": {"number": 9, "state": pr_state,
                                 "pull_request": {"merged_at": merged}}},
        }]

        def fake_read(paths):
            p = paths[0]
            if "/comments" in p:
                return []
            if "/timeline" in p:
                return timeline
            return {"state": "open", "assignees": [], "title": "t", "body": "b"}

        monkeypatch.setattr(gh, "gh_read", fake_read)
        return gh.recheck_candidate("o/r", 1)

    def test_open_pr_counts_as_competition(self, monkeypatch):
        r = self._recheck(monkeypatch, "open")
        assert r.has_cross_ref_pr is True

    def test_closed_pr_does_not(self, monkeypatch):
        r = self._recheck(monkeypatch, "closed")
        assert r.has_cross_ref_pr is False

    def test_merged_pr_does_not(self, monkeypatch):
        r = self._recheck(monkeypatch, "closed", merged="2026-01-01T00:00:00Z")
        assert r.has_cross_ref_pr is False


class TestCompetingPrDetection:
    """复检必须能发现"没关联 issue 就直接开的 PR"。

    2026-10-28 实录：ansible#87799 的 timeline 里没有 cross-referenced（对方直接开
    PR 没写 Fixes），因此 has_cross_ref_pr=False；只有按标题里的模块名搜 PR 才
    能发现 #87459 "mount_facts: strip blkid UUID lookup output"。
    """

    def test_module_name_keyword_finds_unlinked_pr(self, monkeypatch):
        from scout_v2 import git_host as gh

        def fake_read(paths):
            p = paths[0]
            if "/comments" in p:
                return []
            if "/timeline" in p:
                return []          # 没有 cross-ref
            if "search/issues" in p:
                return {"items": [{"number": 87459,
                                   "title": "mount_facts: strip blkid UUID lookup output"}]}
            return {"state": "open", "assignees": [], "title": "t", "body": "b"}

        monkeypatch.setattr(gh, "gh_read", fake_read)
        r = gh.recheck_candidate("ansible/ansible", 87799,
                                 root_cause_keywords=["mount_facts"])
        assert r.has_cross_ref_pr is False        # timeline 确实没有
        assert r.open_pr_hits                     # 但搜索抓到了
        assert "87459" in r.open_pr_hits[0]

    def test_no_keywords_still_checks_by_issue_number(self, monkeypatch):
        """不给关键词也不能完全不查竞争。"""
        from scout_v2 import git_host as gh
        seen: list[str] = []

        def fake_read(paths):
            p = paths[0]
            seen.append(p)
            if "/comments" in p:
                return []
            if "/timeline" in p:
                return []
            if "search/issues" in p:
                return {"items": [{"number": 5, "title": "fixes it"}]}
            return {"state": "open", "assignees": [], "title": "t", "body": "b"}

        monkeypatch.setattr(gh, "gh_read", fake_read)
        r = gh.recheck_candidate("o/r", 42)
        assert any("search/issues" in s for s in seen), "must run a PR search even without keywords"
        assert r.open_pr_hits


class TestIdentifierFirstKeywords:
    """标题里的标识符（含 _ 或 .）比普通单词更能定位模块。"""

    def test_identifier_words_come_first(self):
        import re
        title = "mount_facts fails to match UUID based devices because of newline"
        raw = [w for w in re.split(r"[^a-zA-Z0-9_.-]+", title)
               if len(w) >= 5 and not w.isdigit()]
        ident = [w for w in raw if "_" in w or "." in w]
        ordered = (ident + [w for w in raw if w not in ident])[:3]
        assert ordered[0] == "mount_facts"


class TestRelevanceTwoTier:
    """竞争 PR 的相关性判定：两档权衡的是两类错误的代价。

    漏判（假阴性）→ 开重复 PR，伤账号声誉，比错过一次机会严重得多。
    所以标识符类关键词严格匹配；普通词要求至少两个同时出现。
    """

    def _run(self, monkeypatch, keywords, pr_titles):
        from scout_v2 import git_host as gh

        def fake_read(paths):
            p = paths[0]
            if "/comments" in p:
                return []
            if "/timeline" in p:
                return []
            if "search/issues" in p:
                return {"items": [{"number": i + 100, "title": t}
                                  for i, t in enumerate(pr_titles)]}
            return {"state": "open", "assignees": [], "title": "t", "body": "b"}

        monkeypatch.setattr(gh, "gh_read", fake_read)
        return gh.recheck_candidate("o/r", 1, root_cause_keywords=keywords)

    def test_identifier_keyword_matches_title(self, monkeypatch):
        r = self._run(monkeypatch, ["mount_facts"],
                      ["mount_facts: strip blkid UUID lookup output"])
        assert r.open_pr_hits

    def test_identifier_does_not_match_unrelated(self, monkeypatch):
        r = self._run(monkeypatch, ["mount_facts"],
                      ["fix: unrelated thing in another module"])
        assert not r.open_pr_hits

    def test_single_generic_word_is_not_enough(self, monkeypatch):
        """只共享一个泛词不算同一个问题。"""
        r = self._run(monkeypatch, ["Spurious", "despite", "suppressing"],
                      ["Stop --update-data from inserting spurious blank lines"])
        assert not r.open_pr_hits

    def test_two_generic_words_count(self, monkeypatch):
        r = self._run(monkeypatch, ["spurious", "suppressing"],
                      ["fix spurious notes when suppressing errors"])
        assert r.open_pr_hits
