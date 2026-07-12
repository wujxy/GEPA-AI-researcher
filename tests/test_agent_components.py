import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from gepa_researcher.agent_client import AgentError, ClaudeCodeClient
from gepa_researcher.agent_components import AgentExecutor, AgentJudger, AgentProposer
from gepa_researcher.schemas import Candidate, LoopState, SampleTrace, Trace


class CapturingClient:
    def __init__(self, data):
        self.data = data
        self.prompts = []

    def run_json(self, prompt: str, label: str = "agent"):
        self.prompts.append((label, prompt))
        return type("Result", (), {"text": "{}", "data": self.data})()


class QueuedClient:
    """Fake client that serves a queued response (result or exception) per call.

    Used to drive the executor's repair path: queue an AgentError first, then a
    valid result, and assert the repair transcription call fires.
    """

    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []

    def run_json(self, prompt: str, label: str = "agent"):
        self.prompts.append((label, prompt))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response



class RuntimeQueuedClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def run_json(self, prompt: str, label: str = "agent", call_context=None, **kwargs):
        self.calls.append({"label": label, "prompt": prompt, "call_context": call_context, "kwargs": kwargs})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

def _make_candidate(candidate_id: str = "cand_000") -> Candidate:
    return Candidate(
        candidate_id=candidate_id,
        round_id=0,
        hypothesis="h",
        scope="task_system",
        proposed_change="c",
        rationale="r",
        expected_improvement="e",
        risk="rk",
        prompt_text="",
        created_at="now",
    )


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

    def test_executor_prompt_includes_hardened_delivery_contract(self):
        client = CapturingClient({"summary": "", "errors": [], "artifact_paths": []})
        config = {"task": {"goal": "g"}, "runtime": {}, "evidence": {}}
        with tempfile.TemporaryDirectory() as tmp:
            AgentExecutor(client, Path(tmp)).execute(_make_candidate(), config)
        prompt = client.prompts[0][1]
        self.assertIn("final response MUST be exactly one parseable JSON object", prompt)
        self.assertIn("NEVER finish with natural-language status", prompt)
        self.assertIn("waiting for results", prompt)
        self.assertIn("validation.passed=false", prompt)
        self.assertIn("block until they exit", prompt)
        self.assertIn("artifact_paths", prompt)  # schema still present
        self.assertNotIn("Return only a JSON object, no prose outside JSON.", prompt)

    def test_executor_repair_transcribes_after_non_json(self):
        err = AgentError("Agent did not return a parseable JSON object.")
        err.raw_output = "That's just the run-1 header echoed at start. Waiting for the actual ms/evt results."
        repaired = type(
            "Result",
            (),
            {
                "text": "{}",
                "data": {
                    "summary": "transcribed prior state into JSON",
                    "validation": {"passed": False},
                    "metrics": {"primary": None},
                    "errors": ["prior attempt produced no JSON"],
                    "artifact_paths": [],
                    "diagnostics": [],
                },
            },
        )()
        client = QueuedClient([err, repaired])
        config = {"task": {"goal": "g"}, "runtime": {}, "evidence": {}}
        with tempfile.TemporaryDirectory() as tmp:
            trace = AgentExecutor(client, Path(tmp)).execute(_make_candidate("cand_001"), config)

        # The repair transcription call fired exactly once after the failure.
        self.assertEqual(len(client.prompts), 2)
        repair_prompt = client.prompts[1][1]
        self.assertIn("PREVIOUS attempt", repair_prompt)
        self.assertIn("transcribe the current state", repair_prompt)
        self.assertIn("Waiting for the actual ms/evt results.", repair_prompt)
        self.assertIn("DO NOT", repair_prompt)

        # The trace reflects the repaired data and is stamped with an audit marker.
        sample = trace.samples[0]
        self.assertIn("transcribed prior state into JSON", sample.output)
        self.assertEqual(sample.error, "prior attempt produced no JSON")
        self.assertTrue(sample.artifacts.get("repair_applied"))
        self.assertIn("Waiting for the actual ms/evt results.", sample.artifacts.get("original_raw_output", ""))

    def test_proposer_prompt_uses_role_context_without_full_candidate_pool(self):
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
                "candidate_pool": {"raw": "POOL_RAW_SHOULD_NOT_APPEAR"},
                "pareto_frontier": {"candidate_ids": ["parent"], "parent_ids": ["parent"]},
                "parents": [{"candidate_id": "parent", "hypothesis": "baseline", "artifacts": {"agent_raw": "PARENT_RAW_SHOULD_NOT_APPEAR"}}],
                "score_matrix": {"aggregate_scores": {"parent": 0.4, "unrelated": 0.1}},
                "parent_executions": {"parent": {"raw": "EXECUTION_SHOULD_NOT_APPEAR"}},
                "recent_feedback": ["inspect tail behavior"],
                "recent_traces": [],
                "dataset_split": {"feedback_ids": ["f1"], "pareto_ids": ["p1"]},
            },
        }

        AgentProposer(client).propose_batch(LoopState(task_name="task"), config)

        prompt = client.prompts[0][1]
        self.assertIn("Proposer role context", prompt)
        self.assertIn("Parent candidates", prompt)
        self.assertIn("Score summary", prompt)
        self.assertIn("inspect tail behavior", prompt)
        self.assertNotIn("candidate_pool", prompt)
        self.assertNotIn("POOL_RAW_SHOULD_NOT_APPEAR", prompt)
        self.assertNotIn("PARENT_RAW_SHOULD_NOT_APPEAR", prompt)
        self.assertNotIn("EXECUTION_SHOULD_NOT_APPEAR", prompt)

    def test_executor_prompt_uses_role_context_without_gepa_global_state(self):
        client = CapturingClient({"summary": "", "errors": [], "artifact_paths": []})
        candidate = _make_candidate("cand_010")
        candidate.executor_contract = {"instructions": "run the narrow check"}
        config = {
            "task": {"goal": "g"},
            "runtime": {},
            "evidence": {},
            "_eval_phase": "feedback",
            "_selected_sample_ids": ["f1"],
            "_gepa_context": {
                "score_matrix": {"raw": "SCORE_SHOULD_NOT_APPEAR"},
                "pareto_frontier": {"raw": "FRONTIER_SHOULD_NOT_APPEAR"},
                "recent_feedback": ["FEEDBACK_SHOULD_NOT_APPEAR"],
                "recent_traces": [{"raw": "TRACE_SHOULD_NOT_APPEAR"}],
                "gate_decision": {"raw": "GATE_SHOULD_NOT_APPEAR"},
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            AgentExecutor(client, Path(tmp)).execute(candidate, config)

        prompt = client.prompts[0][1]
        self.assertIn("Candidate decision facts", prompt)
        self.assertIn("Working directory for any scripts/artifacts you create", prompt)
        self.assertIn("Candidate source repository", prompt)
        self.assertIn("run the narrow check", prompt)
        self.assertNotIn("SCORE_SHOULD_NOT_APPEAR", prompt)
        self.assertNotIn("FRONTIER_SHOULD_NOT_APPEAR", prompt)
        self.assertNotIn("FEEDBACK_SHOULD_NOT_APPEAR", prompt)
        self.assertNotIn("TRACE_SHOULD_NOT_APPEAR", prompt)
        self.assertNotIn("GATE_SHOULD_NOT_APPEAR", prompt)

    def test_judger_prompt_excludes_expected_gain_and_prior_context(self):
        client = CapturingClient(
            {
                "score": 0.5,
                "passed": False,
                "per_sample_scores": [{"sample_id": "task_execution", "score": 0.5}],
                "failure_categories": [],
                "actionable_feedback": ["tighten evidence"],
                "confidence": "medium",
                "best_interpretation": "partial evidence",
            }
        )
        candidate = _make_candidate("cand_020")
        candidate.expected_gain = 999.0
        candidate.expected_improvement = "EXPECTED_IMPROVEMENT_SHOULD_NOT_APPEAR"
        trace = Trace(
            candidate_id=candidate.candidate_id,
            round_id=candidate.round_id,
            samples=[
                SampleTrace(
                    sample_id="task_execution",
                    input="in",
                    output="out",
                    expected="unknown",
                    logs="ran validation",
                    artifacts={"metrics": {"primary": 0.5}, "validation": {"passed": False}},
                )
            ],
        )
        config = {
            "task": {"goal": "g"},
            "_run_dir": "/tmp/run",
            "_eval_phase": "pareto",
            "_selected_sample_ids": ["p1"],
            "_prior_context": {"notes": ["PRIOR_SHOULD_NOT_APPEAR"]},
            "_gepa_context": {"score_matrix": {"raw": "SCORE_SHOULD_NOT_APPEAR"}},
        }

        AgentJudger(client).judge(candidate, trace, config)

        prompt = client.prompts[0][1]
        self.assertIn("Trace decision facts", prompt)
        self.assertIn("'primary': 0.5", prompt)
        self.assertNotIn("expected_gain", prompt)
        self.assertNotIn("EXPECTED_IMPROVEMENT_SHOULD_NOT_APPEAR", prompt)
        self.assertNotIn("PRIOR_SHOULD_NOT_APPEAR", prompt)
        self.assertNotIn("SCORE_SHOULD_NOT_APPEAR", prompt)


    def test_executor_repair_skipped_when_disabled(self):
        err = AgentError("Agent did not return a parseable JSON object.")
        err.raw_output = "x"
        client = QueuedClient([err])
        config = {"task": {"goal": "g"}, "runtime": {}, "evidence": {}, "executor": {"repair_retries": 0}}
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(AgentError):
                AgentExecutor(client, Path(tmp)).execute(_make_candidate("cand_002"), config)
        # No repair call when retries are disabled.
        self.assertEqual(len(client.prompts), 1)

    def test_executor_repair_reraises_when_repair_also_fails(self):
        err1 = AgentError("first attempt produced no JSON")
        err1.raw_output = "raw1"
        err2 = AgentError("repair attempt produced no JSON")
        err2.raw_output = "raw2"
        client = QueuedClient([err1, err2])
        config = {"task": {"goal": "g"}, "runtime": {}, "evidence": {}}
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(AgentError):
                AgentExecutor(client, Path(tmp)).execute(_make_candidate("cand_003"), config)
        # Exactly one repair attempt, then the failure propagates.
        self.assertEqual(len(client.prompts), 2)

    def test_executor_repair_uses_same_runtime_launch_options(self):
        first_error = AgentError("not json")
        first_error.raw_output = "partial non-json output"
        result = type("Result", (), {
            "text": "{}",
            "data": {
                "summary": "repair summarized",
                "implementation": {"changed_files": [], "commands_run": [], "notes": ""},
                "metrics": {"primary": None, "baseline": None, "delta": None},
                "validation": {"passed": False, "checks": [], "regressions": []},
                "diagnostics": [],
                "artifact_paths": [],
                "errors": [],
            },
        })()
        client = RuntimeQueuedClient([first_error, result])
        candidate = _make_candidate()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            host_repo = root / "repo"
            host_artifacts = root / "artifacts"
            host_repo.mkdir()
            config = {
                "task": {"goal": "optimize task"},
                "executor": {"repair_retries": 1},
                "_candidate_repo": "/workspace/repo",
                "_candidate_workspace": "/workspace/artifacts",
                "_candidate_repo_host": str(host_repo),
                "_candidate_workspace_host": str(host_artifacts),
                "_executor_host_cwd": str(host_repo),
                "_executor_command_prefix": ["apptainer", "exec"],
                "_executor_inherit_host_env": False,
                "_executor_resolve_command_on_host": False,
                "_candidate_env": {"HOME": "/workspace/home"},
            }

            AgentExecutor(client, root).execute(candidate, config)

        self.assertEqual(len(client.calls), 2)
        for call in client.calls:
            self.assertEqual(call["kwargs"]["cwd"], host_repo)
            self.assertEqual(call["kwargs"]["command_prefix"], ["apptainer", "exec"])
            self.assertFalse(call["kwargs"]["inherit_host_env"])
            self.assertFalse(call["kwargs"]["resolve_command_on_host"])
            self.assertEqual(call["kwargs"]["env"]["HOME"], "/workspace/home")
        self.assertIn("/workspace/repo", client.calls[0]["prompt"])
        self.assertIn("/workspace/artifacts", client.calls[0]["prompt"])
        self.assertIn("/workspace/repo", client.calls[1]["prompt"])
        self.assertIn("/workspace/artifacts", client.calls[1]["prompt"])


if __name__ == "__main__":
    unittest.main()
