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
                "scope": "task_system",
                "proposed_change": "execute a baseline",
                "rationale": "start simple",
                "expected_improvement": "better primary metric",
                "risk": "weak result",
                "strategy": "baseline_strategy",
                "analysis_plan": ["run script"],
            }
        )
        config = {
            "task": {"goal": "optimize task", "data_files": ["data.csv"]},
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
        self.assertNotIn("histogram_with_execute", prompt)

    def test_executor_prompt_includes_runtime(self):
        client = CapturingClient(
            {
                "summary": "ran analysis",
                "implementation": "changed one function and ran validation",
                "validation": {},
                "metrics": {},
                "diagnostics": [],
                "artifact_paths": [],
                "errors": [],
            }
        )
        candidate = Candidate(
            candidate_id="cand_000",
            round_id=0,
            hypothesis="test baseline candidate",
            scope="task_system",
            proposed_change="test baseline candidate model",
            rationale="baseline",
            expected_improvement="execute",
            risk="weak result",
            prompt_text="",
            created_at="now",
        )
        config = {
            "task": {"goal": "optimize task", "data_files": ["data.csv"]},
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
                        "scope": "task_system",
                        "proposed_change": f"change {index}",
                        "rationale": "reason",
                        "expected_improvement": "better primary metric",
                        "risk": "risk",
                        "strategy": "baseline_strategy",
                        "analysis_plan": ["execute"],
                    }
                    for index in range(10)
                ]
            }
        )
        config = {
            "generation": {"batch_size": 10},
            "task": {"goal": "optimize task", "data_files": ["data.csv"]},
            "runtime": {"python_command": "python"},
            "evidence": {},
        }

        batch = AgentProposer(client).propose_batch(LoopState(task_name="task"), config)

        self.assertEqual(len(batch.candidates), 10)
        self.assertEqual(batch.candidates[0].candidate_id, "cand_000_000")
        prompt = client.prompts[0][1]
        self.assertIn("Propose exactly 10 candidate", prompt)
        self.assertIn('"candidates"', prompt)

    def test_agent_proposer_batch_prompt_includes_candidate_policy_targets(self):
        client = CapturingClient(
            {
                "candidates": [
                    {
                        "hypothesis": "hypothesis",
                        "scope": "task_system",
                        "proposed_change": "change",
                        "rationale": "reason",
                        "expected_improvement": "better primary metric",
                        "risk": "risk",
                        "strategy": "safe-pattern #1",
                        "target_files": ["OMILRECV2/src/RecHelper.cc"],
                        "safety_class": "safe",
                        "analysis_plan": ["execute"],
                    }
                ]
            }
        )
        config = {
            "generation": {"batch_size": 1},
            "workspace": {"baseline_ref": "Br1.0.1"},
            "task": {"goal": "optimize task", "data_files": ["data.csv"]},
            "runtime": {"python_command": "python"},
            "evidence": {},
            "candidate_policy": {
                "known_target_files": ["OMILRECV2/src/RecHelper.cc", "OMILRECV2/src/RecHelper.h"],
                "allowed_strategies": ["safe-pattern #1"],
                "allowed_safety_classes": ["safe"],
            },
        }

        AgentProposer(client).propose_batch(LoopState(task_name="task"), config)

        prompt = client.prompts[0][1]
        self.assertIn("Candidate policy", prompt)
        self.assertIn("Source baseline/ref: Br1.0.1", prompt)
        self.assertIn("OMILRECV2/src/RecHelper.cc", prompt)
        self.assertIn("safe-pattern #1", prompt)

    def test_agent_proposer_batch_prompt_includes_recent_traces_and_dataset_split(self):
        client = CapturingClient(
            {
                "candidates": [
                    {
                        "hypothesis": "hypothesis",
                        "scope": "task_system",
                        "proposed_change": "change",
                        "rationale": "reason",
                        "expected_improvement": "better primary metric",
                        "risk": "risk",
                        "strategy": "baseline_strategy",
                        "analysis_plan": ["execute"],
                    }
                ]
            }
        )
        config = {
            "generation": {"batch_size": 1},
            "task": {"goal": "optimize task", "data_files": ["data.csv"]},
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
                                "sample_id": "task_execution",
                                "logs": "ran baseline_strategy execute",
                                "error": "singular matrix",
                                "artifacts": {"metrics": {"primary_metric": 12.3}},
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
        self.assertIn("ran baseline_strategy execute", prompt)
        self.assertIn("singular matrix", prompt)
        self.assertIn("primary_metric", prompt)
        self.assertIn("Dataset split", prompt)
        self.assertIn("pareto_ids", prompt)

    def test_agent_prompts_use_structured_context_before_evidence_refs(self):
        proposer_client = CapturingClient(
            {
                "candidates": [
                    {
                        "hypothesis": "hypothesis",
                        "scope": "task_system",
                        "proposed_change": "change",
                        "rationale": "reason",
                        "expected_improvement": "better primary metric",
                        "risk": "risk",
                        "strategy": "baseline_strategy",
                        "analysis_plan": ["execute"],
                    }
                ]
            }
        )
        proposer_config = {
            "generation": {"batch_size": 1},
            "task": {"goal": "optimize task", "data_files": ["data.csv"]},
            "runtime": {"python_command": "python"},
            "evidence": {},
            "_gepa_context": {
                "pareto_frontier": {},
                "parents": [],
                "score_matrix": {},
                "recent_feedback": [],
                "recent_traces": [
                    {
                        "candidate_id": "cand_000_000",
                        "evidence_refs": ["traces/round_000/cand_000_000/trace.json"],
                        "samples": [{"summary": "test baseline candidate", "key_metrics": {"primary_metric": 770.38}}],
                    }
                ],
                "dataset_split": {},
            },
        }

        AgentProposer(proposer_client).propose_batch(LoopState(task_name="task"), proposer_config)

        proposer_prompt = proposer_client.prompts[0][1]
        self.assertIn("Use structured facts and metrics as the default evidence", proposer_prompt)
        self.assertIn("Only read evidence_refs when the structured context is insufficient", proposer_prompt)
        self.assertIn("traces/round_000/cand_000_000/trace.json", proposer_prompt)

    def test_agent_proposer_prompt_uses_compact_state_without_history_feedback_blob(self):
        client = CapturingClient(
            {
                "candidates": [
                    {
                        "hypothesis": "hypothesis",
                        "scope": "task_system",
                        "proposed_change": "change",
                        "rationale": "reason",
                        "expected_improvement": "better primary metric",
                        "risk": "risk",
                        "strategy": "baseline_strategy",
                        "analysis_plan": ["execute"],
                    }
                ]
            }
        )
        state = LoopState(task_name="task", round_id=2, best_candidate_id="seed_000", best_score=0.95)
        state.history.append(
            {
                "round_id": 1,
                "kept": [],
                "rejected": ["cand_000_000"],
                "next_feedback": ["VERBOSE_FEEDBACK_BLOB_SHOULD_NOT_APPEAR"],
                "best_candidate_id": "seed_000",
                "best_score": 0.95,
                "stop": False,
            }
        )
        config = {
            "generation": {"batch_size": 1},
            "task": {"goal": "optimize task", "data_files": ["data.csv"]},
            "runtime": {"python_command": "python"},
            "evidence": {},
            "_gepa_context": {
                "pareto_frontier": {},
                "parents": [],
                "score_matrix": {},
                "recent_feedback": ["compact feedback"],
                "recent_traces": [],
                "dataset_split": {},
            },
        }

        AgentProposer(client).propose_batch(state, config)

        prompt = client.prompts[0][1]
        self.assertIn("Current state facts", prompt)
        self.assertIn("'best_candidate_id': 'seed_000'", prompt)
        self.assertNotIn("VERBOSE_FEEDBACK_BLOB_SHOULD_NOT_APPEAR", prompt)

    def test_agent_executor_uses_per_candidate_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "fake_claude.py"
            script.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env python3",
                        "print('{\"summary\":\"ran\",\"implementation\":\"x\",\"validation\":{},\"metrics\":{},\"diagnostics\":[],\"artifact_paths\":[],\"errors\":[]}')",
                    ]
                ),
                encoding="utf-8",
            )
            os.chmod(script, 0o755)
            client = ClaudeCodeClient(command=str(script), timeout_seconds=99)
            candidate = Candidate(
                candidate_id="cand_000_000",
                round_id=0,
                hypothesis="test baseline candidate",
                scope="task_system",
                proposed_change="test baseline candidate model",
                rationale="baseline",
                expected_improvement="execute",
                risk="weak result",
                prompt_text="",
                created_at="now",
            )
            config = {
                "_executor_timeout_seconds": 7,
                "task": {"goal": "optimize task", "data_files": ["data.csv"]},
                "runtime": {},
                "evidence": {},
            }
            output = StringIO()

            with redirect_stdout(output):
                AgentExecutor(client, Path(tmp)).execute(candidate, config)

            self.assertIn("timeout=7s", output.getvalue())


if __name__ == "__main__":
    unittest.main()
