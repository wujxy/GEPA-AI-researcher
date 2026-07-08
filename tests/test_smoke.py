import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from gepa_researcher.io_utils import read_json
from gepa_researcher.orchestrator import ResearchOrchestrator


class OrchestratorSmokeTest(unittest.TestCase):
    def test_orchestrator_smoke(self):
        config_path = Path("examples/paper_qa/config.json").resolve()
        config = read_json(config_path)

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            config["run_dir"] = str(run_dir)
            state = ResearchOrchestrator(config=config, config_path=config_path).run()

            self.assertTrue(state.history)
            self.assertIsNotNone(state.best_candidate_id)
            self.assertTrue((run_dir / "final_report.md").exists())
            self.assertTrue((run_dir / "candidates.jsonl").exists())

    def test_orchestrator_prints_progress(self):
        config_path = Path("examples/paper_qa/config.json").resolve()
        config = read_json(config_path)

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            config["run_dir"] = str(run_dir)
            output = StringIO()

            with redirect_stdout(output):
                ResearchOrchestrator(config=config, config_path=config_path).run()

            text = output.getvalue()
            self.assertIn("Starting run", text)
            self.assertIn("Round 1/", text)
            self.assertIn("proposer started", text)
            self.assertIn("Hypothesis:", text)
            self.assertIn("executor finished", text)
            self.assertIn("Judgment:", text)
            self.assertIn("Decision:", text)
            self.assertIn("Artifacts:", text)

    def test_orchestrator_writes_live_candidate_before_round_persistence(self):
        config_path = Path("examples/paper_qa/config.json").resolve()
        config = read_json(config_path)

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            config["run_dir"] = str(run_dir)

            ResearchOrchestrator(config=config, config_path=config_path).run()

            self.assertTrue((run_dir / "live" / "round_000_candidate.json").exists())

    def test_claude_config_uses_conda_myenv_runtime(self):
        config = read_json(Path("examples/function_discovery/config.claude.json"))

        self.assertEqual(config["runtime"]["conda_env"], "myenv")
        self.assertEqual(config["runtime"]["python_command"], "conda run -n myenv python")
        self.assertIn("Bash(conda run -n myenv python *)", config["agent"]["extra_args"])
        self.assertTrue(config["evidence"]["visualize_when_applicable"])
        self.assertEqual(config["evidence"]["plot_selection_policy"], "proposer_selects")
        self.assertNotIn("preferred_plots", config["evidence"])
        self.assertIn("Bash(ls *)", config["agent"]["extra_args"])


if __name__ == "__main__":
    unittest.main()
