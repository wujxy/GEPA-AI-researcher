from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

from gepa_researcher.models.schemas import Candidate, CandidateBatch, Judgment, SampleTrace, Trace
from gepa_researcher.orchestrator import ResearchOrchestrator
from gepa_researcher.storage.pool import CandidatePool


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return completed.stdout.strip()


def _make_synthetic_omilrec_repo(root: Path) -> tuple[Path, str]:
    repo = root / "synthetic-omilrec"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "GEPA Synthetic")
    (repo / "OMILRECV2" / "src").mkdir(parents=True)
    (repo / "benchmarks").mkdir()
    (repo / "tests" / "fixtures" / "v107_rev1").mkdir(parents=True)
    (repo / "scripts").mkdir()
    (repo / "OMILRECV2" / "src" / "hot.cc").write_text(
        "int hot_path() { return 1; }\n",
        encoding="utf-8",
    )
    (repo / "benchmarks" / "speed.csv").write_text("candidate,ms_per_event\nbaseline,10.0\n", encoding="utf-8")
    (repo / "benchmarks" / "drift.csv").write_text("candidate,max_drift\nbaseline,0.0\n", encoding="utf-8")
    (repo / "tests" / "fixtures" / "v107_rev1" / "charge_pdf.bin").write_text(
        "version https://git-lfs.github.com/spec/v1\n",
        encoding="utf-8",
    )
    (repo / "scripts" / "quick_bench.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (repo / "scripts" / "diff_drift.py").write_text("print('ok')\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "baseline")
    baseline = _git(repo, "rev-parse", "HEAD")
    # Mimic the OMILREC pack: LFS fixtures are materialized in the controller
    # repo and therefore appear dirty in each worktree after materialization.
    (repo / "tests" / "fixtures" / "v107_rev1" / "charge_pdf.bin").write_bytes(b"real fixture bytes\n")
    return repo, baseline


class SyntheticOmilrecProposer:
    def propose(self, state, config):
        return self.propose_batch(state, config).candidates[0]

    def propose_batch(self, state, config):
        batch_size = int(config.get("generation", {}).get("batch_size", 1))
        parent_ids = list((config.get("_context_view") or {}).get("envelope", {}).get("parent_ids", []))
        if not parent_ids:
            parent_ids = list((config.get("_gepa_context") or {}).get("pareto_frontier", {}).get("parent_ids", []))
        candidates = []
        for index in range(batch_size):
            candidate_id = f"synthetic_{state.round_id:03d}_{index:03d}"
            target_files = ["OMILRECV2/src/hot.cc"]
            candidates.append(
                Candidate(
                    candidate_id=candidate_id,
                    round_id=state.round_id,
                    parent_ids=parent_ids,
                    hypothesis="cache the hot path",
                    scope="OMILRECV2/src/hot.cc",
                    proposed_change="append a harmless candidate marker",
                    rationale="exercise OMILREC-like execution semantics",
                    expected_improvement="lower ms_per_event",
                    risk="low; source-only change",
                    prompt_text="synthetic omilrec proposal",
                    created_at="now",
                    target_files=target_files,
                    executor_contract={"target_files": target_files},
                    expected_artifacts=["benchmarks/speed.csv", "benchmarks/drift.csv"],
                    strategy="synthetic-safe-pattern",
                )
            )
        return CandidateBatch(round_id=state.round_id, candidates=candidates)


class SyntheticOmilrecExecutor:
    def __init__(self):
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()
        self.modes: list[str] = []

    def execute(self, candidate, config):
        mode = str(config["_execution_mode"])
        repo = Path(config["_candidate_repo"])
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.modes.append(mode)
        try:
            time.sleep(0.05)
            if mode == "implement_and_validate":
                _git(repo, "config", "user.email", "test@example.invalid")
                _git(repo, "config", "user.name", "GEPA Synthetic")
                source = repo / "OMILRECV2" / "src" / "hot.cc"
                source.write_text(
                    source.read_text(encoding="utf-8") + f"// {candidate.candidate_id}\n",
                    encoding="utf-8",
                )
                _git(repo, "add", "OMILRECV2/src/hot.cc")
                _git(repo, "commit", "-m", f"candidate {candidate.candidate_id}")
                commit_sha = _git(repo, "rev-parse", "HEAD")
                artifacts = {"commit_sha": commit_sha, "mode": mode}
            else:
                assert (repo / "tests" / "fixtures" / "v107_rev1" / "charge_pdf.bin").read_bytes() == b"real fixture bytes\n"
                (repo / "benchmarks" / "drift.csv").write_text(
                    f"candidate,max_drift\n{candidate.candidate_id},1e-15\n",
                    encoding="utf-8",
                )
                (repo / "benchmarks" / "speed.csv").write_text(
                    f"candidate,ms_per_event\n{candidate.candidate_id},8.0\n",
                    encoding="utf-8",
                )
                artifacts = {"mode": mode, "generated_tracked": ["benchmarks/drift.csv", "benchmarks/speed.csv"]}
            return Trace(
                candidate_id=candidate.candidate_id,
                round_id=candidate.round_id,
                samples=[
                    SampleTrace(
                        sample_id="local_100evt_speed",
                        input="synthetic input",
                        output="ok",
                        expected="no regression",
                        logs=f"{mode} completed",
                        artifacts=artifacts,
                    )
                ],
            )
        finally:
            with self.lock:
                self.active -= 1


class SyntheticOmilrecJudger:
    def judge(self, candidate, trace, config):
        failed = any(sample.error for sample in trace.samples)
        is_child = bool(candidate.parent_ids)
        score = 0.95 if is_child else 0.75
        return Judgment(
            candidate_id=candidate.candidate_id,
            round_id=candidate.round_id,
            score=0.0 if failed else score,
            passed=not failed,
            per_sample_scores=[
                {"sample_id": sample.sample_id, "score": 0.0 if failed else score}
                for sample in trace.samples
            ],
            failure_categories=["execution_failure"] if failed else [],
            actionable_feedback=["execution failed"] if failed else ["synthetic omilrec gate passed"],
            confidence="high",
        )


def _config(run_dir: Path, repo: Path, baseline: str) -> dict:
    return {
        "resume": False,
        "run_dir": str(run_dir),
        "components": {"mode": "claude_code_agents"},
        "budget": {"max_rounds": 1, "no_improvement_patience": 2},
        "generation": {"batch_size": 2, "enable_merge": False},
        "initialization": {"seed_count": 2},
        "executor": {"max_workers": 2, "executor_timeout_seconds": 30, "fail_fast": False},
        "judger": {"pass_threshold": 0.5},
        "task": {
            "name": "synthetic-omilrec-loop",
            "goal": "exercise OMILREC-like loop stability without JUNO runtime",
            "samples": [
                {"sample_id": "local_100evt_speed"},
                {"sample_id": "fcn_drift"},
            ],
        },
        "gepa": {
            "frontier_policy": "pareto",
            "acceptance_policy": "minibatch_improves_then_pareto",
            "minibatch_size": 1,
            "parent_sampling": "pareto_win_weighted",
            "feedback_sample_ids": ["local_100evt_speed"],
            "pareto_sample_ids": ["local_100evt_speed", "fcn_drift"],
        },
        "workspace": {
            "mode": "git_worktree",
            "repo_path": str(repo),
            "baseline_ref": baseline,
            "pre_materialized_lfs_paths": ["tests/fixtures/v107_rev1/*.bin"],
            "generated_tracked_paths": ["benchmarks/drift.csv", "benchmarks/speed.csv"],
        },
        "candidate_policy": {
            "allowed_target_globs": ["OMILRECV2/src/**/*.cc", "OMILRECV2/src/**/*.h"],
            "frozen_globs": ["tests/**", "scripts/**", "benchmarks/*.md"],
            "max_target_files": 3,
        },
        "context": {"paths": [], "notes": [], "skills": []},
        "usage_tracking": {"enabled": False, "print_round_summary": False, "print_run_summary": False},
    }


def test_synthetic_omilrec_loop_allows_generated_tracked_eval_outputs(tmp_path: Path):
    repo, baseline = _make_synthetic_omilrec_repo(tmp_path)
    run_dir = tmp_path / "run"
    executor = SyntheticOmilrecExecutor()

    state = ResearchOrchestrator(
        _config(run_dir, repo, baseline),
        tmp_path / "synthetic.task.yaml",
        components=(SyntheticOmilrecProposer(), executor, SyntheticOmilrecJudger()),
    ).run()

    pool = CandidatePool.load(run_dir)
    execution_rows = (run_dir / "executions.jsonl").read_text(encoding="utf-8")

    assert state.best_candidate_id is not None
    assert pool.active_ids()
    assert executor.max_active >= 2
    assert "implement_and_validate" in executor.modes
    assert "evaluate_only" in executor.modes
    assert '"phase": "implementation"' in execution_rows
    assert '"phase": "feedback_eval"' in execution_rows
    assert '"phase": "pareto_eval"' in execution_rows
    assert "READONLY_EXECUTION_MUTATED_REPO" not in execution_rows
