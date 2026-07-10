import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from gepa_researcher.io_utils import read_json
from gepa_researcher.orchestrator import ResearchOrchestrator

from tests._fakes import fake_components, make_generic_config


class OrchestratorSmokeTest(unittest.TestCase):
    def _run(self, **overrides):
        tmp = tempfile.mkdtemp()
        run_dir = Path(tmp) / "run"
        config = make_generic_config(run_dir, **overrides)
        buf = StringIO()
        with redirect_stdout(buf):
            state = ResearchOrchestrator(
                config=config,
                config_path=Path(tmp) / "config.json",
                components=fake_components(),
            ).run()
        return state, run_dir, buf.getvalue()

    def test_orchestrator_smoke(self):
        state, run_dir, _ = self._run()

        self.assertTrue(state.history)
        self.assertIsNotNone(state.best_candidate_id)
        self.assertTrue((run_dir / "final_report.md").exists())
        self.assertTrue((run_dir / "candidates.jsonl").exists())

    def test_orchestrator_prints_progress(self):
        _, _, text = self._run()

        self.assertIn("Run Start", text)
        self.assertIn("dataset_split:", text)
        self.assertIn("Round 1/", text)
        self.assertIn("Phase: proposer mutation", text)
        self.assertIn("Proposal:", text)
        self.assertIn("Phase: feedback eval", text)
        self.assertIn("executor running", text)
        self.assertIn("Execution Result:", text)
        self.assertIn("Judgment Result:", text)
        self.assertIn("Gate Decision", text)
        self.assertIn("Generation Summary", text)
        self.assertIn("Run Finish", text)
        self.assertIn("artifacts:", text)

    def test_orchestrator_writes_live_candidate_batch_before_round_persistence(self):
        _, run_dir, _ = self._run()

        self.assertTrue((run_dir / "live" / "round_000_candidate_batch.json").exists())

    def test_claude_config_uses_conda_myenv_runtime(self):
        config = read_json(Path("examples/function_discovery/config.claude.json"))

        self.assertEqual(config["runtime"]["conda_env"], "myenv")
        self.assertEqual(config["runtime"]["python_command"], "conda run -n myenv python")
        self.assertIn("Bash(conda run -n myenv python *)", config["agent"]["extra_args"])
        self.assertEqual(config["generation"]["batch_size"], 5)
        self.assertFalse(config["generation"]["enable_merge"])
        self.assertEqual(config["gepa"]["frontier_policy"], "pareto")
        self.assertEqual(config["gepa"]["acceptance_policy"], "minibatch_improves_then_pareto")
        self.assertEqual(config["gepa"]["parent_sampling"], "pareto_win_weighted")
        self.assertEqual(config["initialization"]["seed_count"], 1)
        self.assertEqual(config["gepa"]["minibatch_size"], 2)
        self.assertEqual(config["executor"]["max_workers"], 3)
        self.assertEqual(config["executor"]["executor_timeout_seconds"], 900)
        self.assertFalse(config["executor"]["fail_fast"])
        self.assertTrue(config["executor"]["per_candidate_workspace"])
        self.assertTrue(config["evidence"]["visualize_when_applicable"])
        self.assertEqual(config["evidence"]["plot_selection_policy"], "proposer_selects")
        self.assertNotIn("preferred_plots", config["evidence"])
        self.assertIn("Bash(ls *)", config["agent"]["extra_args"])


if __name__ == "__main__":
    unittest.main()
