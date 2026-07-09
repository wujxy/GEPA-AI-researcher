import unittest

from gepa_researcher.display import (
    format_agent_action,
    format_gate_summary,
    format_judgment_summary,
    format_phase_header,
    format_proposal_summary,
    format_trace_summary,
)
from gepa_researcher.schemas import Candidate, GateDecision, Judgment, SampleTrace, Trace


class DisplayFormatterTest(unittest.TestCase):
    def _candidate(self):
        return Candidate(
            candidate_id="cand_000_003",
            round_id=0,
            parent_id="seed_000",
            parent_ids=["seed_000"],
            generation=1,
            status="generated",
            hypothesis="execute a compact mixture",
            scope="task_system",
            proposed_change="compare two component mixture against the seed",
            rationale="seed weak results the tail",
            expected_improvement="higher pareto score",
            risk="may overexecute",
            prompt_text="prompt",
            created_at="now",
            executor_contract={"instructions": "run one compact executeting script"},
            mutation_note="responds to tail feedback",
        )

    def test_phase_header_includes_round_phase_and_sample_ids(self):
        text = format_phase_header(0, 5, "feedback eval", ["task_a", "task_b"])

        self.assertIn("Phase: feedback eval", text)
        self.assertIn("Round 1/5", text)
        self.assertIn("sample_ids=['task_a', 'task_b']", text)

    def test_agent_action_names_executor_candidate_and_phase(self):
        text = format_agent_action("executor", "running", "cand_000_003", "feedback")

        self.assertIn("executor running cand_000_003", text)
        self.assertIn("phase=feedback", text)

    def test_proposal_summary_includes_context_and_executor_instruction(self):
        text = format_proposal_summary(self._candidate(), phase="feedback", score=0.42)

        self.assertIn("Proposal: cand_000_003", text)
        self.assertIn("phase: feedback", text)
        self.assertIn("parents: seed_000", text)
        self.assertIn("generation: 1", text)
        self.assertIn("status: generated", text)
        self.assertIn("score: 0.4200", text)
        self.assertIn("hypothesis: execute a compact mixture", text)
        self.assertIn("executor: run one compact executeting script", text)

    def test_trace_summary_includes_metrics_diagnostics_and_errors(self):
        trace = Trace(
            candidate_id="cand_000_003",
            round_id=0,
            samples=[
                SampleTrace(
                    sample_id="task_a",
                    input="data",
                    output="{}",
                    expected="unknown",
                    logs="ran execute",
                    error="bad execute",
                    artifacts={
                        "summary": "execute completed with warning",
                        "implementation": "changed implementation path",
                        "metrics": {"primary_metric": 12.3},
                        "diagnostics": ["tail miss"],
                        "artifact_paths": ["plot.png"],
                    },
                )
            ],
        )

        text = format_trace_summary(trace, phase="feedback", sample_ids=["task_a"])

        self.assertIn("Execution Result: cand_000_003", text)
        self.assertIn("phase: feedback", text)
        self.assertIn("sample_ids: ['task_a']", text)
        self.assertIn("summary: execute completed with warning", text)
        self.assertIn("metrics: {'primary_metric': 12.3}", text)
        self.assertIn("diagnostics: ['tail miss']", text)
        self.assertIn("errors: bad execute", text)

    def test_judgment_summary_includes_score_passed_and_feedback(self):
        judgment = Judgment(
            candidate_id="cand_000_003",
            round_id=0,
            score=0.56,
            passed=False,
            per_sample_scores=[],
            failure_categories=["tail_miss"],
            actionable_feedback=["improve tail execute"],
            confidence="medium",
        )

        text = format_judgment_summary(judgment, phase="feedback")

        self.assertIn("Judgment Result: cand_000_003", text)
        self.assertIn("score: 0.5600", text)
        self.assertIn("passed: False", text)
        self.assertIn("failure_categories: ['tail_miss']", text)
        self.assertIn("feedback: ['improve tail execute']", text)

    def test_gate_summary_includes_accepted_discarded_and_reasons(self):
        decision = GateDecision(
            round_id=0,
            accepted=["cand_000_003"],
            discarded=["cand_000_004"],
            reason_by_candidate={"cand_000_004": "did not improve on feedback"},
        )

        text = format_gate_summary(decision)

        self.assertIn("Gate Decision", text)
        self.assertIn("accepted: cand_000_003", text)
        self.assertIn("discarded: cand_000_004", text)
        self.assertIn("reason[cand_000_004]: did not improve on feedback", text)


if __name__ == "__main__":
    unittest.main()
