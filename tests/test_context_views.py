import json
import tempfile
import unittest
from pathlib import Path

from gepa_researcher.context_views import candidate_for_agent, trace_for_agent, trace_summary_for_proposer
from gepa_researcher.runtime import recent_trace_summaries
from gepa_researcher.schemas import Candidate, SampleTrace, Trace


class ContextViewsTest(unittest.TestCase):
    def _candidate(self):
        return Candidate(
            candidate_id="cand_001_000",
            round_id=1,
            parent_id="seed_000",
            parent_ids=["seed_000"],
            hypothesis="Student-t captures mild excess kurtosis.",
            target_module="distribution_model",
            proposed_change="Fit Student-t and compare AIC/BIC.",
            rationale="Prior feedback suggested heavier tails.",
            expected_improvement="Lower AIC/BIC than normal baseline.",
            risk="Extra df parameter may not be justified.",
            prompt_text="Model family: students_t\nPlan: fit t",
            created_at="now",
            executor_contract={"instructions": "fit t", "expected_artifacts": ["t_fit_results.json"]},
            expected_artifacts=["t_fit_results.json"],
            mutation_note="Responds to excess kurtosis feedback.",
            artifacts={
                "agent_raw": "very long raw proposer output",
                "model_family": "students_t",
                "analysis_plan": ["fit", "compare"],
            },
        )

    def _trace(self):
        return Trace(
            candidate_id="cand_001_000",
            round_id=1,
            samples=[
                SampleTrace(
                    sample_id="observed_numeric_dataset",
                    input="data.csv",
                    output="very long duplicated structured output",
                    expected="unknown",
                    logs="Fitted Student-t; AIC worse than normal.",
                    error=None,
                    artifacts={
                        "agent_raw": "very long raw executor output",
                        "summary": "Fitted Student-t and compared against normal.",
                        "model_expression": "X ~ t(df=23.29, loc=2.42, scale=1.14)",
                        "fit_parameters": {"df": 23.29, "loc": 2.42, "scale": 1.14},
                        "metrics": {"aic": 771.25, "bic": 781.69, "ks_p_value": 0.8381},
                        "diagnostics": [
                            "captures mild heavy tails",
                            "information criteria favor normal",
                            "extra parameter not justified",
                            "fourth diagnostic should be trimmed",
                        ],
                        "artifact_paths": ["t_fit_results.json", "t_qq_plot.png"],
                        "errors": [],
                    },
                )
            ],
        )

    def test_candidate_view_keeps_decision_facts_and_evidence_ref_without_agent_raw(self):
        view = candidate_for_agent(self._candidate(), evidence_refs=["traces/round_001/cand_001_000/candidate.json"])

        self.assertEqual(view["candidate_id"], "cand_001_000")
        self.assertEqual(view["parent_ids"], ["seed_000"])
        self.assertEqual(view["model_family"], "students_t")
        self.assertIn("Fit Student-t", view["proposed_change"])
        self.assertEqual(view["executor_contract"]["instructions"], "fit t")
        self.assertEqual(view["evidence_refs"], ["traces/round_001/cand_001_000/candidate.json"])
        self.assertNotIn("agent_raw", str(view))
        self.assertNotIn("very long raw proposer output", str(view))

    def test_trace_view_keeps_metrics_and_diagnostics_without_raw_or_duplicate_output(self):
        view = trace_for_agent(self._trace(), evidence_refs=["traces/round_001/cand_001_000/trace.json"])

        sample = view["samples"][0]
        self.assertEqual(view["candidate_id"], "cand_001_000")
        self.assertEqual(sample["summary"], "Fitted Student-t and compared against normal.")
        self.assertEqual(sample["metrics"]["aic"], 771.25)
        self.assertEqual(sample["diagnostics"], [
            "captures mild heavy tails",
            "information criteria favor normal",
            "extra parameter not justified",
        ])
        self.assertEqual(view["evidence_refs"], ["traces/round_001/cand_001_000/trace.json"])
        self.assertNotIn("agent_raw", str(view))
        self.assertNotIn("very long duplicated structured output", str(view))

    def test_trace_summary_for_proposer_classifies_parent_comparison(self):
        summary = trace_summary_for_proposer(
            self._trace(),
            parent_id="seed_000",
            parent_score=0.95,
            score=0.6,
            evidence_refs=["traces/round_001/cand_001_000/trace.json"],
        )

        self.assertEqual(summary["candidate_id"], "cand_001_000")
        self.assertEqual(summary["comparison_to_parent"]["verdict"], "worse_than_parent")
        self.assertEqual(summary["comparison_to_parent"]["parent_id"], "seed_000")
        self.assertEqual(summary["samples"][0]["key_metrics"]["aic"], 771.25)
        self.assertEqual(summary["evidence_refs"], ["traces/round_001/cand_001_000/trace.json"])
        self.assertNotIn("agent_raw", str(summary))

    def test_recent_trace_summaries_emit_readable_absolute_evidence_refs(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            trace = self._trace()
            (run_dir / "traces.jsonl").write_text(json.dumps(trace.to_dict()) + "\n", encoding="utf-8")

            summaries = recent_trace_summaries(run_dir)

            self.assertEqual(len(summaries), 1)
            ref = summaries[0]["evidence_refs"][0]
            self.assertTrue(ref.startswith(str(run_dir)))
            self.assertTrue(ref.endswith("traces/round_001/cand_001_000/trace.json"))
            self.assertNotIn("agent_raw", str(summaries[0]))


if __name__ == "__main__":
    unittest.main()
