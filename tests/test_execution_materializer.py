from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gepa_researcher.domain.execution import (
    CapabilityPolicy,
    ExecutionBudget,
    ExecutionPhase,
    ExecutionSpec,
)
from gepa_researcher.execution.materializer import RepositoryMaterializer


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
    repo = root / "source"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "GEPA Test")
    (repo / "src").mkdir()
    (repo / "src" / "hot.cc").write_text("int hot() { return 1; }\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "baseline")
    return repo, _git(repo, "rev-parse", "HEAD")


def _spec(execution_id: str, candidate_id: str, input_revision: str) -> ExecutionSpec:
    return ExecutionSpec(
        execution_id=execution_id,
        run_id="run-001",
        round_id=1,
        candidate_id=candidate_id,
        phase=ExecutionPhase.IMPLEMENTATION,
        input_revision=input_revision,
        dataset_ref=None,
        evaluator_version=None,
        budget=ExecutionBudget(wall_seconds=600),
        capability_policy=CapabilityPolicy(
            repo_writable=True,
            network_allowed=False,
            allowed_tools=("bash", "git"),
            forbidden_paths=(),
        ),
    )


def test_materializer_uses_execution_id_not_candidate_id(tmp_path: Path):
    repo, baseline = _make_repo(tmp_path)
    materializer = RepositoryMaterializer(
        run_dir=tmp_path / "run",
        workspace_config={
            "mode": "git_worktree",
            "repo_path": str(repo),
            "baseline_ref": baseline,
            "root": str(tmp_path / "run" / "sandboxes"),
            "branch_prefix": "gepa/test",
        },
    )

    session_a = materializer.materialize(_spec("exec-a", "cand_same", baseline))
    session_b = materializer.materialize(_spec("exec-b", "cand_same", baseline))

    assert session_a.repo_path != session_b.repo_path
    assert "exec-a" in str(session_a.repo_path)
    assert "exec-b" in str(session_b.repo_path)
    assert session_a.artifact_path != session_b.artifact_path
    assert session_a.input_revision == baseline
    assert session_b.input_revision == baseline
    assert (session_a.repo_path / "src" / "hot.cc").read_text(encoding="utf-8") == "int hot() { return 1; }\n"


def test_materializer_materializes_lfs_paths_and_attributes_dirtiness(tmp_path: Path):
    repo, _ = _make_repo(tmp_path)
    fixture_dir = repo / "tests" / "fixtures" / "v107_rev1"
    fixture_dir.mkdir(parents=True)
    pointer = "version https://git-lfs.github.com/spec/v1\n"
    (fixture_dir / "charge_pdf.bin").write_text(pointer, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fixture pointer")
    baseline = _git(repo, "rev-parse", "HEAD")
    (fixture_dir / "charge_pdf.bin").write_bytes(b"real fixture bytes")
    materializer = RepositoryMaterializer(
        run_dir=tmp_path / "run",
        workspace_config={
            "mode": "git_worktree",
            "repo_path": str(repo),
            "baseline_ref": baseline,
            "root": str(tmp_path / "run" / "sandboxes"),
            "branch_prefix": "gepa/test",
            "pre_materialized_lfs_paths": ["tests/fixtures/v107_rev1/*.bin"],
        },
    )

    session = materializer.materialize(_spec("exec-lfs", "cand_same", baseline))
    audit = materializer.assert_clean_for_execution(session)

    assert (session.repo_path / "tests" / "fixtures" / "v107_rev1" / "charge_pdf.bin").read_bytes() == b"real fixture bytes"
    assert audit["unexpected_dirty"] == []
    assert any("charge_pdf.bin" in line for line in audit["allowed_dirty"])


def test_materializer_rejects_bad_lfs_patterns(tmp_path: Path):
    repo, baseline = _make_repo(tmp_path)
    materializer = RepositoryMaterializer(
        run_dir=tmp_path / "run",
        workspace_config={
            "mode": "git_worktree",
            "repo_path": str(repo),
            "baseline_ref": baseline,
            "root": str(tmp_path / "run" / "sandboxes"),
            "pre_materialized_lfs_paths": ["../outside.bin"],
        },
    )

    with pytest.raises(RuntimeError, match="repo-relative"):
        materializer.materialize(_spec("exec-bad", "cand_same", baseline))
