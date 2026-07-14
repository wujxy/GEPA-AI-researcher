import json
import tempfile
import unittest
from pathlib import Path

from gepa_researcher.loop.context_views import (
    build_executor_context,
    build_judger_context,
    build_proposer_context,
    candidate_for_agent,
    trace_for_agent,
    trace_summary_for_proposer,
)
from gepa_researcher.loop.runtime import recent_trace_summaries
from gepa_researcher.models.schemas import Candidate, LoopState, SampleTrace, Trace


class ContextViewsTest(unittest.TestCase):
    def _candidate(self):
        return Candidate(
            candidate_id="cand_001_000",
            round_id=1,
            parent_ids=["seed_000"],
            hypothesis="Candidate tests a bounded task improvement.",
            scope="task_system",
            proposed_change="Apply candidate change and compare configured metrics.",
            rationale="Prior feedback suggested a focused improvement path.",
            expected_improvement="Improve the configured primary metric over baseline.",
            risk="Added complexity may not be justified.",
            prompt_text="Strategy: candidate_strategy\nPlan: execute candidate",
            created_at="now",
            executor_contract={"instructions": "execute candidate", "expected_artifacts": ["execution_results.json"]},
            expected_artifacts=["execution_results.json"],
            mutation_note="Responds to prior feedback.",
            artifacts={
                "agent_raw": "very long raw proposer output",
                "strategy": "candidate_strategy",
                "analysis_plan": ["execute", "compare"],
            },
        )

    def _trace(self):
        return Trace(
            candidate_id="cand_001_000",
            round_id=1,
            samples=[
                SampleTrace(
                    sample_id="task_execution",
                    input="data.csv",
                    output="very long duplicated structured output",
                    expected="unknown",
                    logs="Executed candidate; metric worse than baseline.",
                    error=None,
                    artifacts={
                        "agent_raw": "very long raw executor output",
                        "summary": "Executed candidate and compared against baseline_strategy.",
                        "implementation": "{'changed_files': [], 'commands_run': ['validate'], 'notes': 'candidate executed'}",
                        "validation": {"passed": True, "checks": ["validate"], "regressions": []},
                        "metrics": {"primary": 0.61, "baseline": 0.95, "delta": -0.34},
                        "diagnostics": [
                            "candidate completed configured execution",
                            "configured metric favors baseline_strategy",
                            "added complexity not justified",
                            "fourth diagnostic should be trimmed",
                        ],
                        "artifact_paths": ["execution_results.json", "diagnostic_artifact.txt"],
                        "errors": [],
                    },
                )
            ],
        )

    def test_candidate_view_keeps_decision_facts_and_evidence_ref_without_agent_raw(self):
        view = candidate_for_agent(self._candidate(), evidence_refs=["traces/round_001/cand_001_000/candidate.json"])

        self.assertEqual(view["candidate_id"], "cand_001_000")
        self.assertEqual(view["parent_ids"], ["seed_000"])
        self.assertEqual(view["strategy"], "candidate_strategy")
        self.assertIn("candidate change", view["proposed_change"])
        self.assertEqual(view["executor_contract"]["instructions"], "execute candidate")
        self.assertEqual(view["evidence_refs"], ["traces/round_001/cand_001_000/candidate.json"])
        self.assertNotIn("agent_raw", str(view))
        self.assertNotIn("very long raw proposer output", str(view))

    def test_trace_view_keeps_metrics_and_diagnostics_without_raw_or_duplicate_output(self):
        view = trace_for_agent(self._trace(), evidence_refs=["traces/round_001/cand_001_000/trace.json"])

        sample = view["samples"][0]
        self.assertEqual(view["candidate_id"], "cand_001_000")
        self.assertEqual(sample["summary"], "Executed candidate and compared against baseline_strategy.")
        self.assertEqual(sample["metrics"]["primary"], 0.61)
        self.assertEqual(sample["diagnostics"], [
            "candidate completed configured execution",
            "configured metric favors baseline_strategy",
            "added complexity not justified",
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
        self.assertEqual(summary["samples"][0]["key_metrics"]["primary"], 0.61)
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

    def test_proposer_context_is_role_scoped_and_deterministic(self):
        state = type("State", (), {})()
        state.task_name = "task"
        state.round_id = 2
        state.best_candidate_id = "parent"
        state.best_score = 0.8
        state.no_improvement_rounds = 1
        state.history = [
            {
                "round_id": 1,
                "kept": ["parent"],
                "rejected": ["discarded"],
                "next_feedback": ["VERBOSE_HISTORY_SHOULD_NOT_APPEAR"],
                "best_candidate_id": "parent",
                "best_score": 0.8,
                "stop": False,
            }
        ]
        config = {
            "_prior_context": {"notes": ["note"], "skills": ["skill"], "documents": [], "warnings": []},
            "_gepa_context": {
                "candidate_pool": {"raw": "POOL_RAW_SHOULD_NOT_APPEAR"},
                "pareto_frontier": {
                    "round_id": 1,
                    "candidate_ids": ["parent"],
                    "parent_ids": ["parent"],
                    "per_task_best": {"task_execution": ["parent"]},
                    "artifacts": {"controller_only": "DROP_ME"},
                },
                "parents": [
                    {
                        "candidate_id": "parent",
                        "round_id": 1,
                        "hypothesis": "baseline",
                        "artifacts": {"agent_raw": "PARENT_RAW_SHOULD_NOT_APPEAR", "analysis_plan": ["compare"]},
                    }
                ],
                "score_matrix": {
                    "round_id": 1,
                    "aggregate_scores": {"parent": 0.8, "unrelated": 0.1},
                    "task_scores": {"task_execution": {"parent": 0.8, "unrelated": 0.1}},
                },
                "parent_executions": {"parent": {"raw": "EXECUTION_SHOULD_NOT_APPEAR"}},
                "recent_feedback": ["compact feedback"],
                "recent_traces": [
                    {
                        "candidate_id": "parent",
                        "samples": [
                            {
                                "sample_id": "task_execution",
                                "logs": "ran parent",
                                "artifacts": {"agent_raw": "TRACE_RAW_SHOULD_NOT_APPEAR", "metrics": {"primary": 0.8}},
                            }
                        ],
                        "evidence_refs": ["trace.json"],
                    }
                ],
                "dataset_split": {"feedback_ids": ["f1"], "pareto_ids": ["p1"], "artifacts": {"source": "config"}},
            },
        }

        first = build_proposer_context(state, config)
        second = build_proposer_context(state, config)

        self.assertEqual(first, second)
        self.assertEqual(first["frontier"]["parent_ids"], ["parent"])
        self.assertEqual(first["recent_feedback"], ["compact feedback"])
        self.assertEqual(first["recent_traces"][0]["evidence_refs"], ["trace.json"])
        self.assertEqual(first["dataset_split"]["pareto_ids"], ["p1"])
        self.assertEqual(first["score_summary"]["aggregate_scores"], {"parent": 0.8})
        self.assertNotIn("candidate_pool", str(first))
        self.assertNotIn("agent_raw", str(first))
        self.assertNotIn("POOL_RAW_SHOULD_NOT_APPEAR", str(first))
        self.assertNotIn("EXECUTION_SHOULD_NOT_APPEAR", str(first))
        self.assertNotIn("unrelated", str(first["score_summary"]))

    def test_executor_context_excludes_global_gepa_state(self):
        candidate = self._candidate()
        config = {
            "_prior_context": {"notes": ["note"], "skills": [], "documents": [], "warnings": []},
            "_eval_phase": "feedback",
            "_selected_sample_ids": ["f1"],
            "_gepa_context": {
                "score_matrix": {"aggregate_scores": {"parent": 0.8}},
                "pareto_frontier": {"parent_ids": ["parent"]},
                "recent_feedback": ["GLOBAL_FEEDBACK_SHOULD_NOT_APPEAR"],
                "recent_traces": [{"candidate_id": "other"}],
                "gate_decision": {"raw": "GATE_SHOULD_NOT_APPEAR"},
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            context = build_executor_context(candidate, config, run_dir, run_dir / "work", run_dir / "repo", "implement_and_validate")

        self.assertEqual(context["evaluation"]["eval_phase"], "feedback")
        self.assertEqual(context["candidate"]["candidate_id"], "cand_001_000")
        self.assertEqual(context["workspace"]["source_repo"].split("/")[-1], "repo")
        self.assertNotIn("score_matrix", str(context))
        self.assertNotIn("pareto_frontier", str(context))
        self.assertNotIn("GLOBAL_FEEDBACK_SHOULD_NOT_APPEAR", str(context))
        self.assertNotIn("GATE_SHOULD_NOT_APPEAR", str(context))

    def test_judger_context_is_blind_to_proposer_expected_gain(self):
        candidate = self._candidate()
        candidate.expected_gain = 123.4
        candidate.expected_improvement = "EXPECTED_IMPROVEMENT_SHOULD_NOT_APPEAR"
        config = {
            "_run_dir": "/tmp/run",
            "_eval_phase": "pareto",
            "_selected_sample_ids": ["p1"],
            "_prior_context": {"notes": ["PRIOR_SHOULD_NOT_APPEAR"]},
            "_gepa_context": {"score_matrix": {"raw": "SCORE_SHOULD_NOT_APPEAR"}},
        }

        context = build_judger_context(candidate, self._trace(), config)

        self.assertEqual(context["evaluation"]["selected_sample_ids"], ["p1"])
        self.assertEqual(context["trace"]["samples"][0]["metrics"]["primary"], 0.61)
        self.assertTrue(context["candidate"]["evidence_refs"][0].endswith("candidate.json"))
        self.assertNotIn("expected_gain", str(context))
        self.assertNotIn("EXPECTED_IMPROVEMENT_SHOULD_NOT_APPEAR", str(context))
        self.assertNotIn("PRIOR_SHOULD_NOT_APPEAR", str(context))
        self.assertNotIn("SCORE_SHOULD_NOT_APPEAR", str(context))

    def test_prebuilt_context_view_overrides_legacy_context_for_all_roles(self):
        candidate = self._candidate()
        trace = self._trace()
        prebuilt = {"role": "proposer", "blocks": [{"block_id": "run:task"}]}
        config = {"_context_view": prebuilt, "_gepa_context": {"raw": "ignored"}}

        self.assertEqual(build_proposer_context(LoopState(task_name="task"), config), prebuilt)
        self.assertEqual(
            build_executor_context(candidate, config, Path("/tmp/run"), Path("/tmp/round"), Path("/tmp/repo"), "evaluate_only"),
            prebuilt,
        )
        self.assertEqual(build_judger_context(candidate, trace, config), prebuilt)



if __name__ == "__main__":
    unittest.main()
