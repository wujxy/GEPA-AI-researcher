from __future__ import annotations

import subprocess
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

import pytest

from gepa_researcher.models.schemas import SampleTrace, Trace
from gepa_researcher.orchestrator import ResearchOrchestrator
from gepa_researcher.storage.candidate_store import CandidateStore
from tests._fakes import FakeJudger, FakeProposer, make_generic_config


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


def _git_config(tmp_path: Path, *, max_rounds: int) -> tuple[dict, Path, str]:
    repo, baseline = _make_repo(tmp_path)
    run_dir = tmp_path / "run"
    config = make_generic_config(run_dir, max_rounds=max_rounds, batch_size=1)
    config["workspace"] = {
        "mode": "git_worktree",
        "repo_path": str(repo),
        "root": str(run_dir / "sandboxes"),
        "baseline_ref": baseline,
        "branch_prefix": "gepa/test",
    }
    config["candidate_policy"] = {"max_commits": 1, "frozen_globs": ["tests/**"]}
    return config, repo, baseline


class NonCommittingExecutor:
    def execute(self, candidate, config):
        return Trace(
            candidate_id=candidate.candidate_id,
            round_id=candidate.round_id,
            samples=[SampleTrace("task_execution", "in", "out", "expected", "no source change")],
        )


class KernelProposer(FakeProposer):
    def propose_batch(self, state, config):
        batch = super().propose_batch(state, config)
        for candidate in batch.candidates:
            candidate.target_files = ["src/hot.cc"]
            candidate.safety_class = "safe"
            candidate.strategy = "safe-pattern #1"
            candidate.executor_contract["target_files"] = ["src/hot.cc"]
        return batch


class KernelCommittingExecutor:
    def execute(self, candidate, config):
        repo = Path(config["_candidate_repo"])
        if config.get("_execution_mode") == "implement_and_validate":
            _git(repo, "config", "user.email", "test@example.invalid")
            _git(repo, "config", "user.name", "GEPA Test")
            token = candidate.candidate_id.replace("-", "_")
            (repo / "src" / "hot.cc").write_text(
                f"int hot() {{ return 2; }} // {token}\n",
                encoding="utf-8",
            )
            _git(repo, "add", "src/hot.cc")
            _git(repo, "commit", "-m", f"candidate {candidate.candidate_id}")
        return Trace(
            candidate_id=candidate.candidate_id,
            round_id=candidate.round_id,
            samples=[SampleTrace("task_execution", "in", "out", "expected", "ok")],
        )


def test_seed_requires_result_revision_before_active_pool(tmp_path: Path):
    config, _, _ = _git_config(tmp_path, max_rounds=0)
    orchestrator = ResearchOrchestrator(
        config=config,
        config_path=tmp_path / "config.json",
        components=(KernelProposer(), NonCommittingExecutor(), FakeJudger()),
    )

    with pytest.raises(RuntimeError, match="no valid seeds"):
        with redirect_stdout(StringIO()):
            orchestrator.run()


def test_generation_child_inherits_parent_result_revision(tmp_path: Path):
    config, _, _ = _git_config(tmp_path, max_rounds=1)
    orchestrator = ResearchOrchestrator(
        config=config,
        config_path=tmp_path / "config.json",
        components=(KernelProposer(), KernelCommittingExecutor(), FakeJudger()),
    )

    with redirect_stdout(StringIO()):
        orchestrator.run()

    store = CandidateStore(orchestrator.run_dir)
    child_cards = store.list_by_round(0)
    assert child_cards
    parent = store.get(child_cards[0].parent_candidate_ids[0])
    assert parent is not None
    assert parent.result_revision is not None
    assert child_cards[0].base_revision == parent.result_revision
