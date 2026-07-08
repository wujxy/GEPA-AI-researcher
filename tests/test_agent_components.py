import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
