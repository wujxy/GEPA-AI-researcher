"""Generic deterministic fakes for orchestrator tests.

Injected via ``ResearchOrchestrator(config, config_path, components=...)`` so the
loop runs end-to-end without Claude. These live in ``tests/`` (test scaffolding),
NOT in the ``gepa_researcher`` package, so the package stays free of any
task-specific or mock component code.
"""
from gepa_researcher.schemas import Candidate, CandidateBatch, Judgment, SampleTrace, Trace


class FakeProposer:
    """Emits candidates; round > 0 candidates carry parent_ids from the gepa context."""

    def propose(self, state, config):
        return self.propose_batch(state, config).candidates[0]

    def propose_batch(self, state, config):
        batch_size = int(config.get("generation", {}).get("batch_size", 1))
        parent_ids = list(
            (config.get("_gepa_context") or {}).get("pareto_frontier", {}).get("parent_ids", [])
        )
        candidates = [
            Candidate(
                candidate_id=f"cand_{state.round_id:03d}_{index:03d}",
                round_id=state.round_id,
                parent_ids=parent_ids,
                hypothesis="h",
                scope="task_system",
                proposed_change="c",
                rationale="r",
                expected_improvement="e",
                risk="rk",
                prompt_text="p",
                created_at="now",
            )
            for index in range(batch_size)
        ]
        return CandidateBatch(round_id=state.round_id, candidates=candidates)


class FakeExecutor:
    def execute(self, candidate, config):
        return Trace(
            candidate_id=candidate.candidate_id,
            round_id=candidate.round_id,
            samples=[
                SampleTrace(
                    sample_id="task_execution",
                    input="in",
                    output="ok",
                    expected="unknown",
                    logs="ran",
                    artifacts={},
                )
            ],
        )


class FakeJudger:
    """Deterministic: the first candidate of each batch scores high, the rest low."""

    def judge(self, candidate, trace, config):
        high = candidate.candidate_id.endswith("_000") or candidate.candidate_id.startswith("seed")
        score = 0.8 if high else 0.2
        return Judgment(
            candidate_id=candidate.candidate_id,
            round_id=candidate.round_id,
            score=score,
            passed=score >= 0.85,
            per_sample_scores=[{"sample_id": "task_execution", "score": score}],
            failure_categories=[] if high else ["weak"],
            actionable_feedback=["ok"] if high else ["improve"],
            confidence="high",
        )


def fake_components():
    return FakeProposer(), FakeExecutor(), FakeJudger()


def make_generic_config(run_dir, max_rounds=3, batch_size=2):
    return {
        "resume": False,
        "run_dir": str(run_dir),
        "components": {"mode": "claude_code_agents"},
        "budget": {"max_rounds": max_rounds, "no_improvement_patience": 3},
        "judger": {"pass_threshold": 0.99},
        "generation": {"batch_size": batch_size, "enable_merge": False},
        "gepa": {
            "frontier_policy": "pareto",
            "acceptance_policy": "minibatch_improves_then_pareto",
            "minibatch_size": 1,
            "parent_sampling": "pareto_win_weighted",
            "feedback_sample_ids": ["t1"],
            "pareto_sample_ids": ["t1", "t2"],
        },
        "executor": {
            "max_workers": 1,
            "executor_timeout_seconds": 30,
            "fail_fast": False,
            "per_candidate_workspace": True,
        },
        "initialization": {"seed_count": 1},
        "task": {
            "name": "generic_test_task",
            "goal": "exercise the loop",
            "samples": [{"sample_id": "t1"}, {"sample_id": "t2"}],
        },
        "context": {"paths": [], "notes": [], "skills": []},
    }
