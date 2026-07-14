import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from gepa_researcher.agents.agent_client import AgentError, ClaudeCodeClient
from gepa_researcher.agents.adapters import JudgerAdapter
from gepa_researcher.agents.agent_components import AgentExecutor, AgentJudger, AgentProposer, AgentProtocolError
from gepa_researcher.context.blocks import ContextBlock, ContextBlockKind, ContextRole, ContextVisibility, SourceRef
from gepa_researcher.context.views import ContextView
from gepa_researcher.loop.context_views import build_executor_context, build_judger_context, build_proposer_context
from gepa_researcher.models.schemas import Candidate, ContextEnvelope, LoopState, SampleTrace, Trace


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
    def test_proposer_uses_context_view_prompt_assembler(self):
        client = CapturingClient(
            {
                "hypothesis": "h",
                "scope": "task_system",
                "proposed_change": "c",
                "rationale": "r",
                "expected_improvement": "e",
                "risk": "rk",
            }
        )
        view = ContextView(
            role=ContextRole.PROPOSER,
            envelope=ContextEnvelope(role="proposer", round_id=0, phase="mutation", run_id="run-1"),
            blocks=[
                ContextBlock(
                    block_id="candidate:cand_001",
                    kind=ContextBlockKind.CANDIDATE_FACT,
                    title="Parent",
                    summary="parent summary",
                    inline_content={"candidate_id": "cand_001"},
                    source_refs=[SourceRef(source_type="candidate", source_id="cand_001")],
                    entity_refs=[],
                    visibility=ContextVisibility.AGENT,
                    role_scope=[ContextRole.PROPOSER],
                )
            ],
            metadata={},
        )

        AgentProposer(client).propose(LoopState(task_name="task"), {"task": {"goal": "g"}, "runtime": {}, "evidence": {}, "_context_view": view})

        prompt = client.prompts[0][1]
        self.assertIn("Context envelope", prompt)
        self.assertIn("sources=[candidate:cand_001]", prompt)

    def test_proposer_batch_uses_serialized_context_view_parent_ids(self):
        client = CapturingClient(
            {
                "candidates": [
                    {
                        "hypothesis": "h",
                        "scope": "task_system",
                        "proposed_change": "c",
                        "rationale": "r",
                        "expected_improvement": "e",
                        "risk": "rk",
                    }
                ]
            }
        )
        view = ContextView(
            role=ContextRole.PROPOSER,
            envelope=ContextEnvelope(role="proposer", round_id=1, phase="mutation"),
            blocks=[],
            metadata={"parent_ids": ["context-parent"]},
        ).to_dict()

        batch = AgentProposer(client).propose_batch(
            LoopState(task_name="task", round_id=1),
            {
                "generation": {"batch_size": 1},
                "task": {"goal": "g"},
                "runtime": {},
                "evidence": {},
                "_context_view": view,
                "_gepa_context": {"pareto_frontier": {"parent_ids": ["legacy-parent"]}},
            },
        )

        self.assertEqual(batch.candidates[0].parent_ids, ["context-parent"])
        self.assertIn("Context envelope", client.prompts[0][1])

    def test_role_contexts_include_hard_context_envelope(self):
        candidate = _make_candidate("cand_env")
        state = LoopState(task_name="task", round_id=2)
        config = {
            "task": {"goal": "g"},
            "run_id": "run-001",
            "_eval_phase": "feedback",
            "_execution_id": "exec-env",
            "_input_revision": "a" * 40,
            "_selected_sample_ids": ["s1"],
            "_gepa_context": {"pareto_frontier": {}, "parents": [], "score_matrix": {}, "dataset_split": {}},
        }
        trace = Trace(candidate_id=candidate.candidate_id, round_id=2, samples=[])

        proposer = build_proposer_context(state, config)["envelope"]
        executor = build_executor_context(candidate, config, Path("/tmp/run"), Path("/tmp/artifacts"), Path("/tmp/repo"), "evaluate_only")["envelope"]
        judger = build_judger_context(candidate, trace, config)["envelope"]

        self.assertEqual(proposer["role"], "proposer")
        self.assertEqual(proposer["round_id"], 2)
        self.assertEqual(executor["role"], "executor")
        self.assertEqual(executor["candidate_id"], "cand_env")
        self.assertEqual(executor["execution_id"], "exec-env")
        self.assertEqual(executor["input_revision"], "a" * 40)
        self.assertEqual(executor["selected_sample_ids"], ["s1"])
        self.assertEqual(judger["role"], "judger")
        self.assertEqual(judger["candidate_id"], "cand_env")

    def test_executor_uses_serialized_context_view(self):
        client = CapturingClient(
            {
                "summary": "ran analysis",
                "implementation": {},
                "validation": {},
                "metrics": {},
                "diagnostics": [],
                "artifact_paths": [],
                "errors": [],
            }
        )
        view = ContextView(
            role=ContextRole.EXECUTOR,
            envelope=ContextEnvelope(role="executor", round_id=0, phase="implementation", candidate_id="cand_ctx"),
            blocks=[
                ContextBlock(
                    block_id="candidate:cand_ctx",
                    kind=ContextBlockKind.CANDIDATE_FACT,
                    title="Candidate",
                    summary="serialized executor context",
                    inline_content={"candidate_id": "cand_ctx"},
                    source_refs=[SourceRef(source_type="candidate", source_id="cand_ctx")],
                    entity_refs=[],
                    visibility=ContextVisibility.AGENT,
                    role_scope=[ContextRole.EXECUTOR],
                )
            ],
            metadata={},
        ).to_dict()

        with tempfile.TemporaryDirectory() as tmp:
            AgentExecutor(client, Path(tmp)).execute(
                _make_candidate("cand_ctx"),
                {"task": {"goal": "g"}, "runtime": {}, "evidence": {}, "_context_view": view},
            )

        prompt = client.prompts[0][1]
        self.assertIn("Context envelope", prompt)
        self.assertIn("serialized executor context", prompt)

    def test_judger_uses_serialized_context_view(self):
        client = CapturingClient(
            {
                "score": 0.4,
                "passed": False,
                "per_sample_scores": [],
                "failure_categories": ["weak"],
                "actionable_feedback": ["try again"],
                "confidence": "medium",
            }
        )
        candidate = _make_candidate("cand_judge_ctx")
        trace = Trace(candidate_id=candidate.candidate_id, round_id=0, samples=[])
        view = ContextView(
            role=ContextRole.JUDGE,
            envelope=ContextEnvelope(role="judger", round_id=0, phase="pareto", candidate_id="cand_judge_ctx"),
            blocks=[
                ContextBlock(
                    block_id="trace:cand_judge_ctx",
                    kind=ContextBlockKind.DERIVED_SUMMARY,
                    title="Trace",
                    summary="serialized judge context",
                    inline_content={"candidate_id": "cand_judge_ctx"},
                    source_refs=[SourceRef(source_type="trace", source_id="cand_judge_ctx")],
                    entity_refs=[],
                    visibility=ContextVisibility.AGENT,
                    role_scope=[ContextRole.JUDGE],
                )
            ],
            metadata={},
        ).to_dict()

        AgentJudger(client).judge(candidate, trace, {"task": {"goal": "g"}, "_context_view": view})

        prompt = client.prompts[0][1]
        self.assertIn("Context envelope", prompt)
        self.assertIn("serialized judge context", prompt)

    def test_judger_adapter_injects_serialized_context_view_when_run_dir_is_available(self):
        candidate = _make_candidate("cand_adapter_ctx")
        trace = Trace(candidate_id=candidate.candidate_id, round_id=0, samples=[])

        class RecordingJudger:
            def __init__(self):
                self.config = None

            def judge(self, candidate, trace, config):
                self.config = config
                return type(
                    "Judgment",
                    (),
                    {
                        "candidate_id": candidate.candidate_id,
                        "round_id": candidate.round_id,
                        "score": 0.2,
                    },
                )()

        recording = RecordingJudger()
        with tempfile.TemporaryDirectory() as tmp:
            JudgerAdapter(recording).evaluate_many(
                [candidate],
                type(
                    "TraceBatch",
                    (),
                    {"round_id": 0, "traces": [trace], "failed_candidate_ids": []},
                )(),
                {"task": {"goal": "g"}, "_run_dir": tmp},
            )

        self.assertEqual(recording.config["_context_view"]["role"], "judge")
        self.assertEqual(recording.config["_context_view"]["envelope"]["candidate_id"], candidate.candidate_id)
        self.assertTrue(
            any(block["block_id"] == f"trace:{candidate.candidate_id}:round:{candidate.round_id}" for block in recording.config["_context_view"]["blocks"])
        )

    def test_proposer_rejects_missing_required_payload_fields(self):
        client = CapturingClient({"hypothesis": "h", "scope": "task_system"})
        config = {"task": {"goal": "g"}, "runtime": {}, "evidence": {}}

        with self.assertRaisesRegex(AgentProtocolError, "missing required proposer field"):
            AgentProposer(client).propose(LoopState(task_name="task"), config)

    def test_executor_repair_payload_is_validated(self):
        err = AgentError("Agent did not return a parseable JSON object.")
        err.raw_output = "partial non-json"
        repaired = type("Result", (), {"text": "{}", "data": {"summary": "still missing schema"}})()
        client = QueuedClient([err, repaired])
        config = {"task": {"goal": "g"}, "runtime": {}, "evidence": {}}

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(AgentProtocolError, "missing required executor field"):
                AgentExecutor(client, Path(tmp)).execute(_make_candidate("cand_protocol"), config)

    def test_judger_score_out_of_range_falls_back_to_protocol_failure(self):
        client = CapturingClient(
            {
                "score": 1.7,
                "passed": True,
                "per_sample_scores": [],
                "failure_categories": [],
                "actionable_feedback": [],
                "confidence": "high",
            }
        )
        candidate = _make_candidate("cand_judge_invalid")
        trace = Trace(candidate_id=candidate.candidate_id, round_id=0, samples=[])

        judgment = AgentJudger(client).judge(candidate, trace, {"task": {"goal": "g"}, "judger": {"repair_retries": 0}})

        self.assertEqual(judgment.score, 0.0)
        self.assertFalse(judgment.passed)
        self.assertEqual(judgment.failure_categories, ["judger_protocol_invalid"])
        self.assertEqual(judgment.confidence, "low")

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
        self.assertIn("Final delivery contract", prompt)
        self.assertIn("exactly one parseable JSON object", prompt)
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
        self.assertIn("Runnable envelope guidance", prompt)
        self.assertIn("host-runtime passthrough plus provided paths", prompt)
        self.assertIn("known-good build/test/benchmark path", prompt)
        self.assertIn("do not silently fall back to older paths", prompt)
        self.assertIn("try the project/host pytest executable", prompt)
        self.assertIn("report the candidate as incomplete", prompt)
        self.assertIn("distinguish infrastructure failure from command-selection failure", prompt)
        self.assertIn("Metric evidence must come from fresh foreground execution", prompt)
        self.assertIn("Do not use historical logs", prompt)
        self.assertIn("configured repeat count", prompt)

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
        self.assertIn("Final delivery contract", prompt)
        self.assertIn("exactly one parseable JSON object", prompt)
        self.assertIn("NEVER say that you have already submitted candidates", prompt)
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
            },
        }

        AgentProposer(client).propose_batch(LoopState(task_name="task"), config)

        prompt = client.prompts[0][1]
        self.assertIn("Candidate policy", prompt)
        self.assertIn("Source baseline/ref: Br1.0.1", prompt)
        self.assertIn("OMILRECV2/src/RecHelper.cc", prompt)

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
        client = CapturingClient({
            "summary": "prompt inspected",
            "implementation": {"changed_files": [], "commands_run": [], "notes": ""},
            "metrics": {},
            "validation": {"passed": False},
            "diagnostics": [],
            "artifact_paths": [],
            "errors": [],
        })
        config = {"task": {"goal": "g"}, "runtime": {}, "evidence": {}}
        with tempfile.TemporaryDirectory() as tmp:
            AgentExecutor(client, Path(tmp)).execute(_make_candidate(), config)
        prompt = client.prompts[0][1]
        self.assertIn("final response MUST be exactly one parseable JSON object", prompt)
        self.assertIn("NEVER finish with natural-language status", prompt)
        self.assertIn("waiting for results", prompt)
        self.assertIn("validation.passed=false", prompt)
        self.assertIn("block until they exit", prompt)
        self.assertIn("In implement_and_validate mode, you MUST create a Git commit", prompt)
        self.assertNotIn("commit budget", prompt)
        self.assertIn("git add --", prompt)
        self.assertIn("git commit", prompt)
        self.assertIn("git rev-parse --show-toplevel", prompt)
        self.assertIn("git rev-parse HEAD", prompt)
        self.assertIn("implementation.commit_sha", prompt)
        self.assertIn("copied exactly from git rev-parse HEAD stdout", prompt)
        self.assertIn("If HEAD still equals the input revision", prompt)
        self.assertIn("If you cannot create the commit", prompt)
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
                    "implementation": {"changed_files": [], "commands_run": [], "notes": ""},
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
        client = CapturingClient({
            "summary": "prompt inspected",
            "implementation": {"changed_files": [], "commands_run": [], "notes": ""},
            "metrics": {},
            "validation": {"passed": False},
            "diagnostics": [],
            "artifact_paths": [],
            "errors": [],
        })
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
        self.assertIn("final response MUST be exactly one parseable JSON object", prompt)
        self.assertIn("NEVER return a judgement report in Markdown", prompt)
        self.assertIn("Markdown table", prompt)
        self.assertIn("comparing the executor evidence against the user's stated task goal", prompt)
        self.assertIn("Do not assign 1.0 merely because all gates are green", prompt)
        self.assertIn("0.95-1.00: exceptional / exceeds goal", prompt)
        self.assertIn("0.50-0.60: barely useful / noisy or marginal", prompt)
        self.assertIn("duplicate with no new value", prompt)
        self.assertIn("Evidence caps are mandatory upper bounds", prompt)
        self.assertIn("Cap at 0.75 if the primary metric was not freshly measured", prompt)
        self.assertIn("Cap at 0.60 if the baseline is unclear", prompt)
        self.assertIn("suspected accumulated improvements", prompt)
        self.assertNotIn("expected_gain", prompt)
        self.assertNotIn("EXPECTED_IMPROVEMENT_SHOULD_NOT_APPEAR", prompt)
        self.assertNotIn("PRIOR_SHOULD_NOT_APPEAR", prompt)
        self.assertNotIn("SCORE_SHOULD_NOT_APPEAR", prompt)


    def test_judger_repair_transcribes_after_non_json(self):
        err = AgentError("Agent did not return a parseable JSON object.")
        err.raw_output = "**JUDGEMENT COMPLETE**\n\nScore: 1.0 (passed)\nFeedback: keep this candidate."
        repaired = type(
            "Result",
            (),
            {
                "text": "{}",
                "data": {
                    "score": 1.0,
                    "passed": True,
                    "per_sample_scores": [{"sample_id": "task_execution", "score": 1.0, "notes": "passed"}],
                    "failure_categories": [],
                    "actionable_feedback": ["keep this candidate"],
                    "confidence": "high",
                },
            },
        )()
        client = QueuedClient([err, repaired])
        candidate = _make_candidate("cand_judge_repair")
        trace = Trace(candidate_id=candidate.candidate_id, round_id=0, samples=[])
        judgment = AgentJudger(client).judge(candidate, trace, {"task": {"goal": "g"}, "judger": {"repair_retries": 1}})

        self.assertEqual(len(client.prompts), 2)
        repair_prompt = client.prompts[1][1]
        self.assertIn("PREVIOUS attempt", repair_prompt)
        self.assertIn("transcribe that judgment", repair_prompt)
        self.assertIn("**JUDGEMENT COMPLETE**", repair_prompt)
        self.assertEqual(judgment.score, 1.0)
        self.assertTrue(judgment.passed)
        self.assertTrue(judgment.artifacts.get("repair_applied"))
        self.assertIn("Score: 1.0", judgment.artifacts.get("original_raw_output", ""))

    def test_judger_repair_failure_falls_back_to_failed_judgment(self):
        err1 = AgentError("first judger attempt produced no JSON")
        err1.raw_output = "**JUDGEMENT COMPLETE**"
        err2 = AgentError("repair attempt produced no JSON")
        err2.raw_output = "still markdown"
        client = QueuedClient([err1, err2])
        candidate = _make_candidate("cand_judge_fallback")
        trace = Trace(candidate_id=candidate.candidate_id, round_id=0, samples=[])

        judgment = AgentJudger(client).judge(candidate, trace, {"task": {"goal": "g"}, "judger": {"repair_retries": 1}})

        self.assertEqual(len(client.prompts), 2)
        self.assertEqual(judgment.score, 0.0)
        self.assertFalse(judgment.passed)
        self.assertEqual(judgment.failure_categories, ["judger_invalid_json"])
        self.assertTrue(judgment.artifacts.get("deterministic"))
        self.assertIn("still markdown", judgment.artifacts.get("repair_raw_output", ""))

    def test_judger_non_json_falls_back_when_repair_disabled(self):
        err = AgentError("judger produced no JSON")
        err.raw_output = "markdown only"
        client = QueuedClient([err])
        candidate = _make_candidate("cand_judge_no_repair")
        trace = Trace(candidate_id=candidate.candidate_id, round_id=0, samples=[])

        judgment = AgentJudger(client).judge(candidate, trace, {"task": {"goal": "g"}, "judger": {"repair_retries": 0}})

        self.assertEqual(len(client.prompts), 1)
        self.assertEqual(judgment.failure_categories, ["judger_invalid_json"])
        self.assertFalse(judgment.passed)



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
