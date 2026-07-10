import json
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from gepa_researcher.schemas import Candidate, GateDecision, Judgment, JudgmentBatch, ParetoFrontier, SampleTrace, ScoreMatrix, Trace


class RecordingExecutor:
    def __init__(self):
        self.workspace_by_candidate = {}

    def execute(self, candidate, config):
        workspace = Path(config["_candidate_workspace"])
        self.workspace_by_candidate[candidate.candidate_id] = workspace
        workspace.mkdir(parents=True, exist_ok=True)
        if candidate.candidate_id.endswith("_001"):
            raise RuntimeError("candidate failed intentionally")
        time.sleep(0.05)
        return Trace(
            candidate_id=candidate.candidate_id,
            round_id=candidate.round_id,
            samples=[
                SampleTrace(
                    sample_id="task_a",
                    input="data.csv",
                    output="ok",
                    expected="unknown",
                    logs="ran",
                    artifacts={"workspace": str(workspace)},
                )
            ],
        )


class ScoreByCandidateJudger:
    def judge(self, candidate, trace, config):
        score = 0.9 if candidate.candidate_id.endswith("_000") else 0.2
        return Judgment(
            candidate_id=candidate.candidate_id,
            round_id=candidate.round_id,
            score=score,
            passed=score >= 0.85,
            per_sample_scores=[{"sample_id": "task_a", "score": score}],
            failure_categories=[] if score >= 0.85 else ["weak_result"],
            actionable_feedback=["keep simple"] if score >= 0.85 else ["improve execute"],
            confidence="high",
        )


class GEPAMiniFlowTest(unittest.TestCase):
    def _candidate(self, candidate_id="cand_000_000", round_id=0, parent_ids=None):
        return Candidate(
            candidate_id=candidate_id,
            round_id=round_id,
            parent_id=parent_ids[0] if parent_ids else None,
            parent_ids=parent_ids or [],
            hypothesis="h",
            scope="task_system",
            proposed_change="change",
            rationale="why",
            expected_improvement="score",
            risk="risk",
            prompt_text="prompt",
            created_at="now",
        )

    def test_executor_adapter_isolates_workspaces_and_records_failures(self):
        from gepa_researcher.adapters import ExecutorAdapter

        candidates = [self._candidate(f"cand_000_{index:03d}") for index in range(4)]
        inner = RecordingExecutor()

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            trace_batch = ExecutorAdapter(inner, run_dir).run_many(
                candidates,
                0,
                {"executor": {"max_workers": 3, "executor_timeout_seconds": 5, "fail_fast": False}},
            )

            self.assertEqual(len(trace_batch.traces), 4)
            self.assertEqual(trace_batch.failed_candidate_ids, ["cand_000_001"])
            self.assertTrue((run_dir / "agent_work" / "round_000" / "cand_000_000").exists())
            self.assertTrue((run_dir / "traces" / "round_000" / "cand_000_001" / "trace.json").exists())
            self.assertTrue((run_dir / "traces.jsonl").exists())

            failed_trace = next(trace for trace in trace_batch.traces if trace.candidate_id == "cand_000_001")
            self.assertIn("candidate failed intentionally", failed_trace.samples[0].error)
            self.assertEqual(len((run_dir / "traces.jsonl").read_text(encoding="utf-8").splitlines()), 4)

    def test_judger_adapter_emits_per_candidate_judgments(self):
        from gepa_researcher.adapters import JudgerAdapter
        from gepa_researcher.schemas import TraceBatch

        candidates = [self._candidate(f"cand_000_{index:03d}") for index in range(3)]
        trace_batch = TraceBatch(
            round_id=0,
            traces=[Trace(candidate_id=candidate.candidate_id, round_id=0, samples=[]) for candidate in candidates],
            failed_candidate_ids=[],
        )

        judgment_batch = JudgerAdapter(ScoreByCandidateJudger()).evaluate_many(candidates, trace_batch, {})

        self.assertEqual(len(judgment_batch.judgments), 3)
        self.assertEqual(judgment_batch.summary["candidate_count"], 3)
        self.assertEqual(judgment_batch.summary["best_candidate_id"], "cand_000_000")

    def test_score_matrix_uses_per_task_scores(self):
        from gepa_researcher.score_matrix import ScoreMatrixBuilder

        judgments = [
            Judgment(
                candidate_id="cand_a",
                round_id=0,
                score=0.7,
                passed=False,
                per_sample_scores=[{"sample_id": "task_1", "score": 0.9}, {"sample_id": "task_2", "score": 0.1}],
                failure_categories=[],
                actionable_feedback=[],
                confidence="medium",
            ),
            Judgment(
                candidate_id="cand_b",
                round_id=0,
                score=0.6,
                passed=False,
                per_sample_scores=[{"sample_id": "task_1", "score": 0.3}, {"sample_id": "task_2", "score": 1.0}],
                failure_categories=[],
                actionable_feedback=[],
                confidence="medium",
            ),
        ]

        matrix = ScoreMatrixBuilder.from_judgments(0, judgments)

        self.assertEqual(matrix.task_scores["task_1"]["cand_a"], 0.9)
        self.assertEqual(matrix.task_scores["task_2"]["cand_b"], 1.0)
        self.assertAlmostEqual(matrix.aggregate_scores["cand_a"], 0.5)

    def test_pareto_selector_keeps_per_task_winners(self):
        from gepa_researcher.pareto import ParetoSelector

        matrix = ScoreMatrix(
            round_id=0,
            task_scores={
                "task_1": {"cand_a": 0.9, "cand_b": 0.3, "cand_c": 0.5},
                "task_2": {"cand_a": 0.1, "cand_b": 1.0, "cand_c": 0.5},
            },
        )

        frontier = ParetoSelector().select(matrix, ["cand_a", "cand_b", "cand_c"])

        self.assertIn("cand_a", frontier.candidate_ids)
        self.assertIn("cand_b", frontier.candidate_ids)
        self.assertEqual(frontier.per_task_best["task_1"], ["cand_a"])
        self.assertEqual(frontier.per_task_best["task_2"], ["cand_b"])

    def test_gepa_gate_accepts_task_best_and_discards_non_improver(self):
        from gepa_researcher.gate import GEPAGate

        candidates = [
            self._candidate("child_good", 1, ["parent"]),
            self._candidate("child_bad", 1, ["parent"]),
        ]
        judgments = [
            Judgment("child_good", 1, 0.8, False, [{"sample_id": "task", "score": 0.8}], [], [], "high"),
            Judgment("child_bad", 1, 0.3, False, [{"sample_id": "task", "score": 0.3}], [], [], "high"),
        ]
        previous = ScoreMatrix(round_id=0, task_scores={"task": {"parent": 0.5}}, aggregate_scores={"parent": 0.5})
        trial = ScoreMatrix(
            round_id=1,
            task_scores={"task": {"parent": 0.5, "child_good": 0.8, "child_bad": 0.3}},
            aggregate_scores={"parent": 0.5, "child_good": 0.8, "child_bad": 0.3},
        )

        decision = GEPAGate().accept_or_discard(1, candidates, judgments, previous, trial, had_active_pool=True)

        self.assertEqual(decision.accepted, ["child_good"])
        self.assertEqual(decision.discarded, ["child_bad"])

    def test_generation_decision_merges_feedback_and_accepted_pareto_feedback(self):
        from gepa_researcher.orchestrator import ResearchOrchestrator
        from gepa_researcher.schemas import LoopState

        orchestrator = ResearchOrchestrator(
            config={
                "budget": {"max_rounds": 3, "no_improvement_patience": 3},
                "judger": {"pass_threshold": 0.99},
                "task": {"name": "task"},
            },
            config_path=Path("config.json"),
        )
        state = LoopState(task_name="task", best_score=0.1)
        feedback_batch = JudgmentBatch(
            round_id=0,
            judgments=[
                Judgment("child_good", 0, 0.6, False, [], [], ["feedback hint"], "high"),
                Judgment("child_bad", 0, 0.2, False, [], [], ["discarded feedback"], "high"),
            ],
            summary={},
        )
        pareto_batch = JudgmentBatch(
            round_id=0,
            judgments=[
                Judgment("child_good", 0, 0.7, False, [], [], ["pareto hint"], "high"),
                Judgment("child_bad", 0, 0.3, False, [], [], ["discarded pareto feedback"], "high"),
            ],
            summary={},
        )

        decision = orchestrator._generation_decision_from_gate(
            state,
            0,
            GateDecision(0, accepted=["child_good"], discarded=["child_bad"], reason_by_candidate={}),
            feedback_batch,
            pareto_batch,
            ScoreMatrix(round_id=0, aggregate_scores={"child_good": 0.7}),
            ParetoFrontier(round_id=0, candidate_ids=["child_good"], per_task_best={}),
        )

        self.assertEqual(decision.next_feedback, ["feedback hint", "pareto hint"])

    def test_generation_decision_uses_pareto_feedback_when_no_candidate_accepted(self):
        from gepa_researcher.orchestrator import ResearchOrchestrator
        from gepa_researcher.schemas import LoopState

        orchestrator = ResearchOrchestrator(
            config={
                "budget": {"max_rounds": 3, "no_improvement_patience": 3},
                "judger": {"pass_threshold": 0.99},
                "task": {"name": "task"},
            },
            config_path=Path("config.json"),
        )
        state = LoopState(task_name="task", best_score=0.1)
        feedback_batch = JudgmentBatch(
            round_id=0,
            judgments=[Judgment("child_bad", 0, 0.2, False, [], [], ["feedback hint"], "high")],
            summary={},
        )
        pareto_batch = JudgmentBatch(
            round_id=0,
            judgments=[Judgment("child_bad", 0, 0.3, False, [], [], ["pareto improver hint"], "high")],
            summary={},
        )

        decision = orchestrator._generation_decision_from_gate(
            state,
            0,
            GateDecision(0, accepted=[], discarded=["child_bad"], reason_by_candidate={}),
            feedback_batch,
            pareto_batch,
            ScoreMatrix(round_id=0, aggregate_scores={"parent": 0.5}),
            ParetoFrontier(round_id=0, candidate_ids=["parent"], per_task_best={}),
        )

        self.assertEqual(decision.next_feedback, ["feedback hint", "pareto improver hint"])

    def test_orchestrator_writes_gepa_artifacts_and_parent_ids(self):
        from gepa_researcher.orchestrator import ResearchOrchestrator
        from tests._fakes import fake_components, make_generic_config

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            config = make_generic_config(run_dir)
            with redirect_stdout(StringIO()):
                state = ResearchOrchestrator(
                    config=config,
                    config_path=Path(tmp) / "config.json",
                    components=fake_components(),
                ).run()

            self.assertTrue(state.history)
            self.assertTrue((run_dir / "candidate_pool.json").exists())
            self.assertTrue((run_dir / "score_matrix.json").exists())
            self.assertTrue((run_dir / "frontier.json").exists())
            self.assertTrue((run_dir / "accepted_candidates.jsonl").exists())
            self.assertTrue((run_dir / "discarded_candidates.jsonl").exists())

            candidate_lines = [
                json.loads(line)
                for line in (run_dir / "candidates.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            second_round = [row for row in candidate_lines if row["round_id"] == 1]
            self.assertTrue(second_round)
            self.assertTrue(any(row["parent_ids"] for row in second_round))


if __name__ == "__main__":
    unittest.main()
