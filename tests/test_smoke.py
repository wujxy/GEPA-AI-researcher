import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from gepa_researcher.storage.io_utils import read_json
from gepa_researcher.models.schemas import Candidate, CandidateBatch
from gepa_researcher.orchestrator import ResearchOrchestrator

from tests._fakes import FakeExecutor, FakeJudger, fake_components, make_generic_config


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


    def test_run_dir_template_uses_local_run_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous = os.environ.get("GEPA_RUN_ID")
            os.environ["GEPA_RUN_ID"] = "local_br101_test"
            try:
                config = make_generic_config(Path(tmp) / "runs" / "<run-id>")
                orchestrator = ResearchOrchestrator(
                    config=config,
                    config_path=Path(tmp) / "config.json",
                    components=fake_components(),
                )
                self.assertEqual(orchestrator.run_dir.name, "local_br101_test")
            finally:
                if previous is None:
                    os.environ.pop("GEPA_RUN_ID", None)
                else:
                    os.environ["GEPA_RUN_ID"] = previous

    def test_resume_false_rejects_stale_initialized_run_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            config = make_generic_config(run_dir, max_rounds=0)
            with redirect_stdout(StringIO()):
                ResearchOrchestrator(
                    config=config,
                    config_path=Path(tmp) / "config.json",
                    components=fake_components(),
                ).run()
            with self.assertRaisesRegex(RuntimeError, "resume=false"):
                with redirect_stdout(StringIO()):
                    ResearchOrchestrator(
                        config=config,
                        config_path=Path(tmp) / "config.json",
                        components=fake_components(),
                    ).run()

    def test_initialization_tops_up_to_seed_count(self):
        class ShortSeedProposer:
            def __init__(self):
                self.single_calls = 0

            def _candidate(self, note):
                return Candidate(
                    candidate_id=f"raw_{note}",
                    round_id=-1,
                    hypothesis="h",
                    scope="task_system",
                    proposed_change="c",
                    rationale="r",
                    expected_improvement="e",
                    risk="rk",
                    prompt_text="p",
                    created_at="now",
                )

            def propose_batch(self, state, config):
                return CandidateBatch(round_id=state.round_id, candidates=[self._candidate("batch")])

            def propose(self, state, config):
                self.single_calls += 1
                return self._candidate(f"single_{self.single_calls}")

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            config = make_generic_config(run_dir, max_rounds=0)
            config["initialization"]["seed_count"] = 3
            proposer = ShortSeedProposer()
            with redirect_stdout(StringIO()):
                ResearchOrchestrator(
                    config=config,
                    config_path=Path(tmp) / "config.json",
                    components=(proposer, FakeExecutor(), FakeJudger()),
                ).run()
            batch = read_json(run_dir / "traces" / "round_-01" / "candidate_batch.json")
            self.assertEqual([c["candidate_id"] for c in batch["candidates"]], ["seed_000", "seed_001", "seed_002"])
            self.assertEqual(proposer.single_calls, 2)

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
        self.assertEqual(config["initialization"]["seed_count"], 3)
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
