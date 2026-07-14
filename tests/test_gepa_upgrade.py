import tempfile
import unittest
from pathlib import Path

from gepa_researcher.loop.context import load_prior_context
from gepa_researcher.loop.gate import GEPAGate
from gepa_researcher.loop.pareto import ParetoSelector
from gepa_researcher.loop.runtime import config_for_eval, resolve_dataset_split, select_feedback_minibatch
from gepa_researcher.models.schemas import Candidate, Judgment, JudgmentBatch, ScoreMatrix
from gepa_researcher.loop.score_matrix import ScoreMatrixBuilder


class GEPAUpgradeTest(unittest.TestCase):
    def test_dataset_split_explicit_and_minibatch(self):
        config = {
            "gepa": {"feedback_sample_ids": ["a", "b"], "pareto_sample_ids": ["c"], "minibatch_size": 1},
            "task": {"samples": [{"sample_id": "a"}, {"sample_id": "b"}, {"sample_id": "c"}]},
        }
        split = resolve_dataset_split(config)
        self.assertEqual(split.feedback_ids, ["a", "b"])
        self.assertEqual(split.pareto_ids, ["c"])
        self.assertEqual(select_feedback_minibatch(split, 0, 1), ["a"])
        self.assertEqual(select_feedback_minibatch(split, 1, 1), ["b"])

    def test_config_for_eval_filters_samples_and_marks_phase(self):
        config = {"task": {"samples": [{"sample_id": "a"}, {"sample_id": "b"}]}}
        selected = config_for_eval(config, ["b"], "feedback", {"notes": ["n"]})
        self.assertEqual(selected["_eval_phase"], "feedback")
        self.assertEqual(selected["_selected_sample_ids"], ["b"])
        self.assertEqual(selected["task"]["samples"], [{"sample_id": "b"}])
        self.assertEqual(selected["_prior_context"]["notes"], ["n"])

    def test_score_matrix_ignores_feedback_phase(self):
        judgment = Judgment("cand", 0, 0.7, False, [{"sample_id": "task", "score": 0.7}], [], [], "high")
        feedback = JudgmentBatch(0, [judgment], {}, {"phase": "feedback"})
        pareto = JudgmentBatch(0, [judgment], {}, {"phase": "pareto", "sample_ids": ["task"]})
        self.assertFalse(ScoreMatrixBuilder.from_batch(feedback).task_scores)
        self.assertEqual(ScoreMatrixBuilder.from_batch(pareto).task_scores["task"]["cand"], 0.7)

    def test_pareto_weighted_parent_sampling_uses_win_counts(self):
        matrix = ScoreMatrix(
            round_id=0,
            task_scores={
                "t1": {"a": 1.0, "b": 0.0},
                "t2": {"a": 1.0, "b": 0.0},
                "t3": {"a": 0.0, "b": 1.0},
            },
        )
        frontier = ParetoSelector().select(matrix, ["a", "b"])
        self.assertEqual(frontier.artifacts["win_counts"]["a"], 2)
        sampled = ParetoSelector().sample_parent_ids(frontier, 2, seed=0)
        self.assertEqual(set(sampled), {"a", "b"})

    def test_gate_requires_minibatch_improvement(self):
        parent = Judgment("parent", 0, 0.5, False, [{"sample_id": "f", "score": 0.5}], [], [], "high")
        good = Judgment("good", 0, 0.6, True, [{"sample_id": "f", "score": 0.6}], [], [], "high")
        bad = Judgment("bad", 0, 0.4, False, [{"sample_id": "f", "score": 0.4}], [], [], "high")
        candidates = [self._candidate("good"), self._candidate("bad")]
        improvers = GEPAGate().minibatch_improvers(candidates, [good, bad], {"parent": parent})
        self.assertEqual([candidate.candidate_id for candidate in improvers], ["good"])

    def test_context_loader_reads_notes_and_warns_missing_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "note.md").write_text("important prior", encoding="utf-8")
            context = load_prior_context(
                {"context": {"paths": ["note.md", "missing.md"], "notes": ["prefer simple"], "skills": ["python"]}},
                base,
            )
        self.assertEqual(context["notes"], ["prefer simple"])
        self.assertEqual(context["skills"], ["python"])
        self.assertIn("important prior", context["documents"][0]["text"])
        self.assertTrue(context["warnings"])

    def _candidate(self, candidate_id):
        return Candidate(
            candidate_id=candidate_id,
            round_id=0,
            parent_ids=["parent"],
            hypothesis="h",
            scope="m",
            proposed_change="c",
            rationale="r",
            expected_improvement="e",
            risk="risk",
            prompt_text="p",
            created_at="now",
        )


if __name__ == "__main__":
    unittest.main()
