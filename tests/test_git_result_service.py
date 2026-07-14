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


def _spec(phase: ExecutionPhase, baseline: str) -> ExecutionSpec:
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


def test_readonly_guard_ignores_untracked_runtime_debris(tmp_path: Path):
    repo, baseline = _make_repo(tmp_path)
    session = _session(tmp_path, repo, baseline)
    service = GitResultService(candidate_policy={})
    before = service.snapshot(session)
    (repo / "build.log").write_text("runtime debris\n", encoding="utf-8")

    service.assert_readonly_unchanged(_spec(ExecutionPhase.FEEDBACK_EVAL, baseline), session, before)
