from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gepa_researcher.domain.execution import CapabilityPolicy, ExecutionBudget, ExecutionPhase, ExecutionSpec
from gepa_researcher.execution.git_result import GitResultService
from gepa_researcher.execution.sandbox import SandboxSession


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return completed.stdout.strip()


def _make_repo(root: Path) -> tuple[Path, str]:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "GEPA Test")
    (repo / "src").mkdir()
    (repo / "src" / "hot.cc").write_text("int hot() { return 1; }\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "fixture.root").write_text("fixture\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "baseline")
    return repo, _git(repo, "rev-parse", "HEAD")


def _session(root: Path, repo: Path, baseline: str) -> SandboxSession:
    artifacts = root / "artifacts"
    scratch = root / "scratch"
    artifacts.mkdir()
    scratch.mkdir()
    return SandboxSession(
        execution_id="exec-1",
        repo_path=repo,
        artifact_path=artifacts,
        scratch_path=scratch,
        input_revision=baseline,
        mode="git_worktree",
        temporary_paths=(repo, artifacts, scratch),
    )


def _spec(phase: ExecutionPhase, baseline: str, *, allowed_target_files: tuple[str, ...] = ()) -> ExecutionSpec:
    return ExecutionSpec(
        execution_id="exec-1",
        run_id="run-001",
        round_id=0,
        candidate_id="cand_000",
        phase=phase,
        input_revision=baseline,
        dataset_ref=None,
        evaluator_version=None,
        budget=ExecutionBudget(wall_seconds=600),
        capability_policy=CapabilityPolicy(
            repo_writable=phase == ExecutionPhase.IMPLEMENTATION,
            network_allowed=False,
            allowed_tools=("bash", "git"),
            forbidden_paths=("tests/**",),
            allowed_target_files=allowed_target_files,
        ),
    )


def test_finalize_records_result_revision_and_frozen_violation(tmp_path: Path):
    repo, baseline = _make_repo(tmp_path)
    session = _session(tmp_path, repo, baseline)
    (repo / "tests" / "fixture.root").write_text("tampered\n", encoding="utf-8")
    _git(repo, "add", "tests/fixture.root")
    _git(repo, "commit", "-m", "candidate")
    service = GitResultService(candidate_policy={"frozen_globs": ["tests/**"]})

    result_sha, audit = service.finalize_implementation(_spec(ExecutionPhase.IMPLEMENTATION, baseline), session)

    assert result_sha == audit.result_sha
    assert "tests/fixture.root" in audit.frozen_violations


def test_readonly_guard_rejects_head_change(tmp_path: Path):
    repo, baseline = _make_repo(tmp_path)
    session = _session(tmp_path, repo, baseline)
    service = GitResultService(candidate_policy={})
    before = service.snapshot(session)
    (repo / "src" / "hot.cc").write_text("int hot() { return 2; }\n", encoding="utf-8")
    _git(repo, "add", "src/hot.cc")
    _git(repo, "commit", "-m", "readonly violation")

    with pytest.raises(RuntimeError, match="read-only execution changed sandbox"):
        service.assert_readonly_unchanged(_spec(ExecutionPhase.FEEDBACK_EVAL, baseline), session, before)


def test_readonly_guard_rejects_tracked_file_change_without_commit(tmp_path: Path):
    repo, baseline = _make_repo(tmp_path)
    session = _session(tmp_path, repo, baseline)
    service = GitResultService(candidate_policy={})
    before = service.snapshot(session)
    (repo / "src" / "hot.cc").write_text("int hot() { return 3; }\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="read-only execution changed sandbox"):
        service.assert_readonly_unchanged(_spec(ExecutionPhase.PARETO_EVAL, baseline), session, before)


def test_readonly_guard_allows_declared_generated_tracked_output(tmp_path: Path):
    repo, baseline = _make_repo(tmp_path)
    (repo / "benchmarks").mkdir()
    (repo / "benchmarks" / "speed.csv").write_text("candidate,score\nbaseline,1\n", encoding="utf-8")
    _git(repo, "add", "benchmarks/speed.csv")
    _git(repo, "commit", "-m", "tracked benchmark")
    baseline = _git(repo, "rev-parse", "HEAD")
    session = _session(tmp_path, repo, baseline)
    service = GitResultService(candidate_policy={"readonly_allowed_dirty_globs": ["benchmarks/speed.csv"]})
    before = service.snapshot(session)
    (repo / "benchmarks" / "speed.csv").write_text("candidate,score\ncandidate,2\n", encoding="utf-8")

    service.assert_readonly_unchanged(_spec(ExecutionPhase.PARETO_EVAL, baseline), session, before)


def test_readonly_guard_ignores_untracked_runtime_debris(tmp_path: Path):
    repo, baseline = _make_repo(tmp_path)
    session = _session(tmp_path, repo, baseline)
    service = GitResultService(candidate_policy={})
    before = service.snapshot(session)
    (repo / "build.log").write_text("runtime debris\n", encoding="utf-8")

    service.assert_readonly_unchanged(_spec(ExecutionPhase.FEEDBACK_EVAL, baseline), session, before)


def test_fallback_commit_matches_globstar_files_directly_under_package(tmp_path: Path):
    repo, baseline = _make_repo(tmp_path)
    (repo / "tinyalgo").mkdir()
    (repo / "tinyalgo" / "paircount.py").write_text("def count():\n    return 1\n", encoding="utf-8")
    _git(repo, "add", "tinyalgo/paircount.py")
    _git(repo, "commit", "-m", "add tiny package")
    baseline = _git(repo, "rev-parse", "HEAD")
    session = _session(tmp_path, repo, baseline)
    (repo / "tinyalgo" / "paircount.py").write_text("def count():\n    return 2\n", encoding="utf-8")
    service = GitResultService(candidate_policy={"allowed_target_globs": ["tinyalgo/**/*.py"]})

    result_sha, audit = service.finalize_implementation(_spec(ExecutionPhase.IMPLEMENTATION, baseline), session)

    assert result_sha != baseline
    assert audit.harness_commit_created is True
    assert audit.harness_committed_files == ["tinyalgo/paircount.py"]
    assert audit.commit_failure_reason is None


# --- §4.8 harness-owned commit tests (A.7) -------------------------------------
# The agent no longer commits; the harness stages only allowed target files
# and commits. These cover the three typed NoCandidateCommit sub-causes, the
# dirty-commit fix, agent-staged-debris reset, and target_files precedence.


def test_harness_commit_stages_only_allowed_target_files(tmp_path: Path):
    """Agent dirties an allowed source file AND a disallowed build output;
    the harness commits only the allowed one."""
    repo, baseline = _make_repo(tmp_path)
    session = _session(tmp_path, repo, baseline)
    (repo / "src" / "hot.cc").write_text("int hot() { return 9; }\n", encoding="utf-8")
    (repo / "build.log").write_text("debris\n", encoding="utf-8")
    service = GitResultService(
        candidate_policy={"allowed_target_globs": ["src/**"], "frozen_globs": ["tests/**"]}
    )

    result_sha, audit = service.finalize_implementation(
        _spec(ExecutionPhase.IMPLEMENTATION, baseline), session
    )

    assert result_sha != baseline
    assert audit.harness_commit_created is True
    assert audit.harness_committed_files == ["src/hot.cc"]
    changed = _git(repo, "diff", "--name-only", baseline, result_sha).splitlines()
    assert "src/hot.cc" in changed
    assert "build.log" not in changed


def test_harness_commit_drops_dirty_benchmark_output(tmp_path: Path):
    """Reproduces test5.log seed_001: agent committed a benchmark output
    alongside the source. The harness must stage only the source."""
    repo, baseline = _make_repo(tmp_path)
    (repo / "benchmarks").mkdir()
    (repo / "benchmarks" / "speed.csv").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "benchmarks/speed.csv")
    _git(repo, "commit", "-m", "track benchmark")
    baseline = _git(repo, "rev-parse", "HEAD")
    session = _session(tmp_path, repo, baseline)
    (repo / "src" / "hot.cc").write_text("int hot() { return 2; }\n", encoding="utf-8")
    (repo / "benchmarks" / "speed.csv").write_text("faster\n", encoding="utf-8")
    service = GitResultService(
        candidate_policy={
            "allowed_target_globs": ["src/**"],
            "frozen_globs": ["tests/**"],
            "readonly_allowed_dirty_globs": ["benchmarks/speed.csv"],
        }
    )

    result_sha, audit = service.finalize_implementation(
        _spec(ExecutionPhase.IMPLEMENTATION, baseline), session
    )

    assert audit.harness_committed_files == ["src/hot.cc"]
    changed = _git(repo, "diff", "--name-only", baseline, result_sha).splitlines()
    assert "benchmarks/speed.csv" not in changed
    assert "src/hot.cc" in changed


def test_harness_commit_resets_agent_staged_debris(tmp_path: Path):
    """Agent ran `git add build.log`; the harness resets the index and commits
    only the allowed source change."""
    repo, baseline = _make_repo(tmp_path)
    session = _session(tmp_path, repo, baseline)
    (repo / "src" / "hot.cc").write_text("int hot() { return 7; }\n", encoding="utf-8")
    (repo / "build.log").write_text("agent staged this\n", encoding="utf-8")
    _git(repo, "add", "build.log")
    service = GitResultService(
        candidate_policy={"allowed_target_globs": ["src/**"], "frozen_globs": ["tests/**"]}
    )

    result_sha, audit = service.finalize_implementation(
        _spec(ExecutionPhase.IMPLEMENTATION, baseline), session
    )

    assert audit.harness_committed_files == ["src/hot.cc"]
    changed = _git(repo, "diff", "--name-only", baseline, result_sha).splitlines()
    assert "build.log" not in changed


def test_harness_commit_empty_tree_yields_empty_reason(tmp_path: Path):
    """Agent changed nothing at all -> commit_failure_reason='empty'."""
    repo, baseline = _make_repo(tmp_path)
    session = _session(tmp_path, repo, baseline)
    service = GitResultService(
        candidate_policy={"allowed_target_globs": ["src/**"], "frozen_globs": ["tests/**"]}
    )

    result_sha, audit = service.finalize_implementation(
        _spec(ExecutionPhase.IMPLEMENTATION, baseline), session
    )

    assert result_sha == baseline
    assert audit.harness_commit_created is False
    assert audit.commit_failure_reason == "empty"


def test_harness_commit_only_frozen_yields_only_forbidden_reason(tmp_path: Path):
    """Agent edited only a frozen path -> 'only_forbidden'."""
    repo, baseline = _make_repo(tmp_path)
    session = _session(tmp_path, repo, baseline)
    (repo / "tests" / "fixture.root").write_text("tampered\n", encoding="utf-8")
    service = GitResultService(
        candidate_policy={"allowed_target_globs": ["src/**"], "frozen_globs": ["tests/**"]}
    )

    result_sha, audit = service.finalize_implementation(
        _spec(ExecutionPhase.IMPLEMENTATION, baseline), session
    )

    assert result_sha == baseline
    assert audit.commit_failure_reason == "only_forbidden"


def test_harness_commit_none_allowed_yields_none_allowed_reason(tmp_path: Path):
    """Agent edited a file that matches no allowed glob and is not frozen ->
    'none_allowed'."""
    repo, baseline = _make_repo(tmp_path)
    session = _session(tmp_path, repo, baseline)
    (repo / "docs").mkdir()
    (repo / "docs" / "notes.md").write_text("out of scope\n", encoding="utf-8")
    service = GitResultService(
        candidate_policy={"allowed_target_globs": ["src/**"], "frozen_globs": ["tests/**"]}
    )

    result_sha, audit = service.finalize_implementation(
        _spec(ExecutionPhase.IMPLEMENTATION, baseline), session
    )

    assert result_sha == baseline
    assert audit.commit_failure_reason == "none_allowed"


def test_harness_commit_uses_target_files_when_globs_empty(tmp_path: Path):
    """When allowed_target_globs is empty but the spec carries per-candidate
    allowed_target_files, the harness stages exactly those files."""
    repo, baseline = _make_repo(tmp_path)
    session = _session(tmp_path, repo, baseline)
    (repo / "src" / "hot.cc").write_text("int hot() { return 5; }\n", encoding="utf-8")
    (repo / "src" / "other.cc").write_text("int other() {}\n", encoding="utf-8")
    # no globs configured; rely on per-candidate target_files
    service = GitResultService(candidate_policy={})

    result_sha, audit = service.finalize_implementation(
        _spec(ExecutionPhase.IMPLEMENTATION, baseline, allowed_target_files=("src/hot.cc",)),
        session,
    )

    assert result_sha != baseline
    assert audit.harness_committed_files == ["src/hot.cc"]
    changed = _git(repo, "diff", "--name-only", baseline, result_sha).splitlines()
    assert changed == ["src/hot.cc"]
