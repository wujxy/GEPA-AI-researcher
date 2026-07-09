import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from gepa_researcher.agent_client import ClaudeCodeClient
from gepa_researcher.agent_components import AgentExecutor, AgentProposer
from gepa_researcher.schemas import Candidate, LoopState


class CapturingClient:
    def __init__(self, data):
        self.data = data
        self.prompts = []

    def run_json(self, prompt: str, label: str = "agent"):
        self.prompts.append((label, prompt))
        return type("Result", (), {"text": "{}", "data": self.data})()


class AgentComponentsTest(unittest.TestCase):
    def test_proposer_prompt_includes_runtime(self):
        client = CapturingClient(
            {
                "hypothesis": "test a compact model",
                "target_module": "distribution_model",
                "proposed_change": "fit a baseline",
                "rationale": "start simple",
                "expected_improvement": "better fit",
                "risk": "underfit",
                "model_family": "normal",
                "analysis_plan": ["run script"],
            }
        )
        config = {
            "task": {"goal": "infer model", "data_files": ["data.csv"]},
            "runtime": {
                "environment": "conda",
                "conda_env": "myenv",
                "python_command": "conda run -n myenv python",
                "dependency_policy": "use installed packages only; do not install packages",
            },
            "evidence": {
                "visualize_when_applicable": True,
                "plot_selection_policy": "proposer_selects",
            },
        }

        AgentProposer(client).propose(LoopState(task_name="task"), config)

        prompt = client.prompts[0][1]
        self.assertIn("Runtime environment", prompt)
        self.assertIn("conda run -n myenv python", prompt)
        self.assertIn("do not install packages", prompt)
        self.assertIn("Visual evidence", prompt)
        self.assertIn("proposer should choose", prompt)
        self.assertNotIn("histogram_with_fit", prompt)

    def test_executor_prompt_includes_runtime(self):
        client = CapturingClient(
            {
                "summary": "ran analysis",
                "model_expression": "x ~ normal(mu, sigma)",
                "fit_parameters": {},
                "metrics": {},
                "diagnostics": [],
                "artifact_paths": [],
                "errors": [],
            }
        )
        candidate = Candidate(
            candidate_id="cand_000",
            round_id=0,
            parent_id=None,
            hypothesis="fit normal",
            target_module="distribution_model",
            proposed_change="fit normal model",
            rationale="baseline",
            expected_improvement="fit",
            risk="underfit",
            prompt_text="",
            created_at="now",
        )
        config = {
            "task": {"goal": "infer model", "data_files": ["data.csv"]},
            "runtime": {
                "environment": "conda",
                "conda_env": "myenv",
                "python_command": "conda run -n myenv python",
                "dependency_policy": "use installed packages only; do not install packages",
                "allowed_commands": ["conda run -n myenv python", "cat", "head", "mkdir"],
            },
            "evidence": {
                "visualize_when_applicable": True,
                "plot_selection_policy": "proposer_selects",
            },
        }

        with tempfile.TemporaryDirectory() as tmp:
            AgentExecutor(client, Path(tmp)).execute(candidate, config)

        prompt = client.prompts[0][1]
        self.assertIn("Runtime environment", prompt)
        self.assertIn("conda run -n myenv python", prompt)
        self.assertIn("Do not install new packages", prompt)
        self.assertIn("Visual evidence", prompt)
        self.assertIn("artifact_paths", prompt)
        self.assertIn("candidate's visual evidence plan", prompt)

    def test_agent_proposer_requests_candidate_batch(self):
        client = CapturingClient(
            {
                "candidates": [
                    {
                        "hypothesis": f"hypothesis {index}",
                        "target_module": "distribution_model",
                        "proposed_change": f"change {index}",
                        "rationale": "reason",
                        "expected_improvement": "better fit",
                        "risk": "risk",
                        "model_family": "normal",
                        "analysis_plan": ["fit"],
                    }
                    for index in range(10)
                ]
            }
        )
        config = {
            "generation": {"batch_size": 10},
            "task": {"goal": "infer model", "data_files": ["data.csv"]},
            "runtime": {"python_command": "python"},
            "evidence": {},
        }

        batch = AgentProposer(client).propose_batch(LoopState(task_name="task"), config)

        self.assertEqual(len(batch.candidates), 10)
        self.assertEqual(batch.candidates[0].candidate_id, "cand_000_000")
        prompt = client.prompts[0][1]
        self.assertIn("Propose exactly 10 candidate", prompt)
        self.assertIn('"candidates"', prompt)

    def test_agent_proposer_batch_prompt_includes_recent_traces_and_dataset_split(self):
        client = CapturingClient(
            {
                "candidates": [
                    {
                        "hypothesis": "hypothesis",
                        "target_module": "distribution_model",
                        "proposed_change": "change",
                        "rationale": "reason",
                        "expected_improvement": "better fit",
                        "risk": "risk",
                        "model_family": "normal",
                        "analysis_plan": ["fit"],
                    }
                ]
            }
        )
        config = {
            "generation": {"batch_size": 1},
            "task": {"goal": "infer model", "data_files": ["data.csv"]},
            "runtime": {"python_command": "python"},
            "evidence": {},
            "_gepa_context": {
                "pareto_frontier": {"candidate_ids": ["parent"], "parent_ids": ["parent"]},
                "parents": [{"candidate_id": "parent", "hypothesis": "baseline"}],
                "score_matrix": {"aggregate_scores": {"parent": 0.4}},
                "recent_feedback": ["inspect tail behavior"],
                "recent_traces": [
                    {
                        "candidate_id": "cand_000_000",
                        "round_id": 0,
                        "samples": [
                            {
                                "sample_id": "observed_numeric_dataset",
                                "logs": "ran normal fit",
                                "error": "singular matrix",
                                "artifacts": {"metrics": {"aic": 12.3}},
                            }
                        ],
                    }
                ],
                "dataset_split": {"feedback_ids": ["f1"], "pareto_ids": ["p1", "p2"]},
            },
        }

        AgentProposer(client).propose_batch(LoopState(task_name="task"), config)

        prompt = client.prompts[0][1]
        self.assertIn("Recent traces", prompt)
        self.assertIn("cand_000_000", prompt)
        self.assertIn("ran normal fit", prompt)
        self.assertIn("singular matrix", prompt)
        self.assertIn("aic", prompt)
        self.assertIn("Dataset split", prompt)
        self.assertIn("pareto_ids", prompt)

    def test_agent_executor_uses_per_candidate_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "fake_claude.py"
            script.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env python3",
                        "print('{\"summary\":\"ran\",\"model_expression\":\"x\",\"fit_parameters\":{},\"metrics\":{},\"diagnostics\":[],\"artifact_paths\":[],\"errors\":[]}')",
                    ]
                ),
                encoding="utf-8",
            )
            os.chmod(script, 0o755)
            client = ClaudeCodeClient(command=str(script), timeout_seconds=99)
            candidate = Candidate(
                candidate_id="cand_000_000",
                round_id=0,
                parent_id=None,
                hypothesis="fit normal",
                target_module="distribution_model",
                proposed_change="fit normal model",
                rationale="baseline",
                expected_improvement="fit",
                risk="underfit",
                prompt_text="",
                created_at="now",
            )
            config = {
                "_executor_timeout_seconds": 7,
                "task": {"goal": "infer model", "data_files": ["data.csv"]},
                "runtime": {},
                "evidence": {},
            }
            output = StringIO()

            with redirect_stdout(output):
                AgentExecutor(client, Path(tmp)).execute(candidate, config)

            self.assertIn("timeout=7s", output.getvalue())


if __name__ == "__main__":
    unittest.main()
