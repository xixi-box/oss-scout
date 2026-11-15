"""Regression coverage for Nov 15 Wasmtime/Qwen delivery classification failures.

The Rust runners below are controlled stand-ins, not claims of rerunning Wasmtime.
Actual Python execution additionally checks unchanged-base test-only replay.
"""
import json
import sys
from pathlib import Path

import pytest

from scout_v2.workers import repair
from test_p5_repair import make_repo, job


def commit(repo):
    repair._git(["add", "-A"], repo)
    repair._git(["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "fixture"], repo)
    return repair._git(["rev-parse", "HEAD"], repo).stdout.strip()


def producer_for(changes, plan):
    class Client:
        def run(self, model, prompt):
            assert "original test unchanged on base" in (self.workdir / repair.TASK_FILE).read_text(encoding="utf-8")
            for path, content in changes.items():
                target = self.workdir / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            (self.workdir / repair.RESULT_FILE).write_text(json.dumps(plan), encoding="utf-8")
            return True, "completed"

    def produce(task, work, context):
        client = Client()
        client.workdir = work
        return repair.opencode_patch_producer(task, work, context, client=client, model="fixture")
    return produce


@pytest.mark.parametrize("agent", ["opencode", "codex"])
@pytest.mark.parametrize("filename,run", [
    ("p1_file_read_write.rs", 43703751196),
    ("p1_fd_readdir.rs", 43704687592),
])
def test_wasmtime_standalone_program_is_seeded_without_adapter_fix(tmp_path, monkeypatch, agent, filename, run):
    repo, _ = make_repo(tmp_path)
    path = "crates/test-programs/src/bin/" + filename
    target = repo / path
    target.parent.mkdir(parents=True)
    (repo / "crates/test-programs/Cargo.toml").write_text(
        '[package]\nname = "test-programs"\nversion = "0.0.0"\npublish = false\n', encoding="utf-8")
    target.write_text("fn main() {}\n", encoding="utf-8")
    adapter = "crates/wasi-preview1-component-adapter/src/lib.rs"
    (repo / adapter).parent.mkdir(parents=True)
    (repo / adapter).write_text("fn offset() -> u8 { 0 }\n", encoding="utf-8")
    sha = commit(repo)
    case = filename.removesuffix(".rs")
    regression = 'fn main() { assert_eq!(offset(), 1); }\n'
    plan = {"test_file": path, "test_cmd": f"cargo test -p wasmtime-wasi --test all {case}",
            "setup_steps": [], "base_failure": [case, "assertion `left == right` failed"],
            "patched_success": [f"{case} ... ok"]}
    observed = []

    def verify(cmd, work, env):
        assert (work / path).read_text() == regression
        fixed = "{ 1 }" in (work / adapter).read_text()
        observed.append(fixed)
        return (0, f"{case} ... ok", "") if fixed else (1, case + '\nassertion `left == right` failed', "")

    monkeypatch.setattr(repair, "_run_cmd", verify)
    task = job(sha)
    task.repair_agent = agent
    out = tmp_path / "out"
    result = repair.run_repair(task, repo, out, patch_producer=producer_for(
        {path: regression, adapter: "fn offset() -> u8 { 1 }\n"}, plan))
    assert result.outcome == "succeeded", (run, result.unverified_items)
    assert observed == [False, True]
    patch = (out / "change.patch").read_text()
    assert path in patch and adapter in patch
    assert json.loads((out / "test-report.json").read_text())["repair_kind"] == "source_fix"


@pytest.mark.parametrize("manifest", [
    '[package]\nname="production"\npublish=false\n',
    '[package]\nname="test-programs"\npublish=true\n',
    'invalid toml',
])
def test_test_program_requires_original_nonpublishable_test_crate(tmp_path, manifest):
    repo, _ = make_repo(tmp_path)
    root = repo / "crates/test-programs"
    root.mkdir(parents=True)
    (root / "Cargo.toml").write_text(manifest)
    sha = commit(repo)
    (root / "Cargo.toml").write_text('[package]\nname="test-programs"\npublish=false\n')
    assert not repair._is_rust_test_program("crates/test-programs/src/bin/example.rs", repo, sha)
    assert not repair._is_rust_test_program("crates/runtime/src/bin/example.rs", repo, sha)


@pytest.mark.parametrize("agent", ["opencode", "codex"])
def test_pure_test_fix_replays_original_then_repaired_same_case(tmp_path, agent):
    repo, _ = make_repo(tmp_path)
    # A real test-fixture bug: the program contract is division, but fixture
    # construction used the wrong denominator. Keep the behavior assertion.
    path = "test_fixture.py"
    original = "def test_fraction():\n    numerator, denominator = 7, 3\n    assert numerator / denominator == 3.5\n"
    (repo / path).write_text(original)
    sha = commit(repo)
    fixed = original.replace("7, 3", "7, 2")
    plan = {"test_file": path, "test_cmd": [sys.executable.replace(chr(92), "/"), "-m", "pytest", path, "-v", "--junitxml=regression.xml"],
            "setup_steps": [], "base_failure": ["test_fraction", "assert (7 / 3) == 3.5"],
            "patched_success": [], "test_report": {"format": "junit", "path": "regression.xml", "case": "test_fraction"}}
    task = job(sha)
    task.repair_agent = agent
    out = tmp_path / "out"
    result = repair.run_repair(task, repo, out, patch_producer=producer_for({path: fixed}, plan))
    assert result.outcome == "succeeded", result.unverified_items
    report = json.loads((out / "test-report.json").read_text())
    assert report["base_returncode"] == 1 and report["patched_returncode"] == 0
    assert report["repair_kind"] == "test_only" and report["test_repair_files"] == [path]
    patch = (out / "change.patch").read_text()
    assert "-    numerator, denominator = 7, 3" in patch
    assert "calc.py" not in patch and "regression.xml" not in patch


@pytest.mark.parametrize("path,run,old,new", [
    ("packages/core/src/code-mode/code-mode.test.ts", 43697476032, "const deadline = 25;\n", "const deadline = 5000;\n"),
    ("integrations/external-context-mem0/src/delete.integration.test.ts", 43694375812,
     "await deleteEntry();\nassertDeleted();\n", "await warmup();\nawait deleteEntry();\nassertDeleted();\n"),
])
def test_qwen_existing_test_repairs_are_not_injected_into_base(tmp_path, path, run, old, new):
    repo, _ = make_repo(tmp_path)
    (repo / path).parent.mkdir(parents=True)
    (repo / path).write_text(old)
    sha = commit(repo)
    plan = {"test_file": path, "test_cmd": "npx vitest run " + path}
    produced = producer_for({path: new}, plan)(job(sha), repo, "recorded run " + str(run))
    assert produced["repair_kind"] == "test_only"
    assert produced["mixed_tests"][path] == (old, new)
    assert path not in produced["test_files"]
    assert path in produced["patch"]
    # Classification is not quality approval: actual replay/reviewer still required.


def test_only_new_tests_still_cannot_substitute_for_a_fix(tmp_path):
    repo, sha = make_repo(tmp_path)
    with pytest.raises(RuntimeError, match="no source change"):
        producer_for({"test_new.py": "def test_new(): assert True\n"},
                     {"test_file": "test_new.py", "test_cmd": "pytest test_new.py"})(job(sha), repo, "")


def test_test_only_inline_rust_runs_original_module_on_base(tmp_path):
    repo, _ = make_repo(tmp_path)
    path = "module.rs"
    old = "fn value() {}\n#[cfg(test)]\nmod tests {\n    fn regression() { fixture(0); }\n}\n"
    new = old.replace("fixture(0)", "fixture(1)")
    (repo / path).write_text(old)
    sha = commit(repo)
    produced = producer_for({path: new}, {"test_file": path, "test_cmd": "cargo test regression"})(job(sha), repo, "")
    assert produced["mixed_tests"][path] == (old, new)
    assert produced["repair_kind"] == "test_only"


def test_recorded_agent_plans_retain_their_actual_evidence_limits():
    records = json.loads((Path(__file__).parent / "fixtures/nov15_test_repairs.json").read_text(encoding="utf-8"))
    assert len(records) == 4
    for record in records:
        # These are the actual last result objects recovered from Actions events.
        # #15297 omitted its base assertion even with JUnit: do not invent one or
        # claim classification alone makes this historical delivery publishable.
        expected = not record["source_run"].endswith("43704687592")
        assert repair._verification_plan_valid(record) is expected


@pytest.mark.parametrize("mode", ["base_already_passes", "renamed_case"])
def test_test_only_repairs_still_require_original_failure_and_identical_case(tmp_path, mode):
    repo, _ = make_repo(tmp_path)
    path = "test_fixture.py"
    old = "def test_target():\n    value = 1\n    assert value == 1\n"
    if mode == "renamed_case":
        old = old.replace("value = 1", "value = 0")
    new = old.replace("value = 0", "value = 1") + "# repaired fixture\n"
    if mode == "renamed_case":
        new = new.replace("test_target", "test_replacement")
    (repo / path).write_text(old)
    sha = commit(repo)
    plan = {"test_file": path, "test_cmd": [sys.executable.replace(chr(92), "/"), "-m", "pytest", path, "--junitxml=regression.xml"],
            "setup_steps": [], "base_failure": ["test_target", "assert 0 == 1"], "patched_success": [],
            "test_report": {"format": "junit", "path": "regression.xml", "case": "test_target"}}
    result = repair.run_repair(job(sha), repo, tmp_path / "out", patch_producer=producer_for({path: new}, plan))
    assert result.outcome != "succeeded"
    assert not result.fixed_on_patch
