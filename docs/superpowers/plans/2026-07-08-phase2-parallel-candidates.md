# Phase 2 Parallel Candidates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade the phase1 single-candidate loop into one generation with multiple candidates, parallel executor execution, batch judgment, top-k gating, and complete archive persistence.

**Architecture:** Preserve the existing phase1 single-candidate interfaces for compatibility, and add batch-native dataclasses plus thin batch wrappers. `ResearchOrchestrator.run_generation()` becomes the new round-level unit: proposer creates `CandidateBatch`, `ParallelExecutor` isolates and executes each candidate, `BatchJudger` emits per-candidate judgments plus a summary, `TopKGater` selects top-k and returns generation feedback, and the orchestrator archives every artifact.

**Tech Stack:** Python 3.10+, standard-library `dataclasses`, `concurrent.futures.ThreadPoolExecutor`, existing `unittest` tests, existing JSON/JSONL helpers.

---

## File Structure

- Modify `gepa_researcher/schemas.py`
  - Add `CandidateBatch`, `TraceBatch`, `JudgmentBatch`, and `GenerationDecision`.
  - Keep existing `Candidate`, `Trace`, `Judgment`, and `Decision` untouched for compatibility.
- Modify `gepa_researcher/proposer.py`
  - Add `RuleBasedProposer.propose_batch()` for local tests and compatibility.
- Modify `gepa_researcher/agent_components.py`
  - Add `AgentProposer.propose_batch()`.
  - Update `AgentExecutor.execute()` workspace convention to include `cand_YYY`.
  - Add optional batch-aware methods for agent prompts where needed.
- Create `gepa_researcher/parallel_executor.py`
  - Implement `ParallelExecutor`.
  - Write per-candidate trace artifacts under `traces/round_XXX/cand_YYY/trace.json`.
  - Append every trace to `traces.jsonl`.
  - Capture failures as trace records without failing the full generation.
- Create `gepa_researcher/batch_judger.py`
  - Implement `BatchJudger`.
  - Generate per-candidate judgments plus round summary artifacts.
- Create `gepa_researcher/topk_gater.py`
  - Implement `TopKGater`.
  - Select top-k candidates by score and aggregate next feedback.
- Modify `gepa_researcher/orchestrator.py`
  - Add `run_generation(round_id, state)`.
  - Use batch flow in `run()`.
  - Persist all candidate, trace, judgment, and decision records.
  - Keep final report readable for multi-candidate rounds.
- Modify `examples/function_discovery/config.claude.json`
  - Add `generation.batch_size=10`, `generation.top_k=3`.
  - Add `executor.max_parallel_executors=3`, `executor.executor_timeout_seconds=900`, `executor.fail_fast=false`, `executor.per_candidate_workspace=true`.
- Add `tests/test_phase2_batch_flow.py`
  - Cover batch schema, proposer count, parallel execution, failure isolation, trace archive, judgment batch, top-k gate, and orchestrator batch generation.
- Update existing tests only where phase1 assumptions must be made compatible with batch mode.

---

### Task 1: Batch Schemas

**Files:**
- Modify: `gepa_researcher/schemas.py`
- Test: `tests/test_phase2_batch_flow.py`

- [ ] **Step 1: Write the failing schema test**

```python
import unittest

from gepa_researcher.schemas import (
    Candidate,
    CandidateBatch,
    GenerationDecision,
    Judgment,
    JudgmentBatch,
    Trace,
    TraceBatch,
)


class Phase2BatchFlowTest(unittest.TestCase):
    def _candidate(self, candidate_id="cand_000_000", round_id=0):
        return Candidate(
            candidate_id=candidate_id,
            round_id=round_id,
            parent_id=None,
            hypothesis="h",
            target_module="distribution_model",
            proposed_change="change",
            rationale="why",
            expected_improvement="score",
            risk="risk",
            prompt_text="prompt",
            created_at="now",
        )

    def test_batch_schema_round_trips_to_dict(self):
        candidate = self._candidate()
        trace = Trace(candidate_id=candidate.candidate_id, round_id=0, samples=[])
        judgment = Judgment(
            candidate_id=candidate.candidate_id,
            round_id=0,
            score=0.7,
            passed=False,
            per_sample_scores=[],
            failure_categories=["missing_metric"],
            actionable_feedback=["add metric"],
            confidence="medium",
        )

        candidate_batch = CandidateBatch(round_id=0, candidates=[candidate])
        trace_batch = TraceBatch(round_id=0, traces=[trace], failed_candidate_ids=[])
        judgment_batch = JudgmentBatch(
            round_id=0,
            judgments=[judgment],
            summary={"candidate_count": 1, "best_score": 0.7},
        )
        decision = GenerationDecision(
            round_id=0,
            kept=["cand_000_000"],
            rejected=[],
            next_feedback=["preserve metric"],
            stop=False,
            artifacts={"best_score": 0.7},
        )

        self.assertEqual(candidate_batch.to_dict()["candidates"][0]["candidate_id"], "cand_000_000")
        self.assertEqual(trace_batch.to_dict()["failed_candidate_ids"], [])
        self.assertEqual(judgment_batch.to_dict()["summary"]["best_score"], 0.7)
        self.assertEqual(decision.to_dict()["kept"], ["cand_000_000"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_phase2_batch_flow.Phase2BatchFlowTest.test_batch_schema_round_trips_to_dict -v`

Expected: FAIL with `ImportError` or `cannot import name 'CandidateBatch'`.

- [ ] **Step 3: Implement minimal schemas**

Add to `gepa_researcher/schemas.py`:

```python
@dataclass
class CandidateBatch:
    round_id: int
    candidates: list[Candidate]
    artifacts: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TraceBatch:
    round_id: int
    traces: list[Trace]
    failed_candidate_ids: list[str]
    artifacts: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class JudgmentBatch:
    round_id: int
    judgments: list[Judgment]
    summary: dict[str, Any]
    artifacts: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GenerationDecision:
    round_id: int
    kept: list[str]
    rejected: list[str]
    next_feedback: list[str]
    stop: bool
    artifacts: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_phase2_batch_flow.Phase2BatchFlowTest.test_batch_schema_round_trips_to_dict -v`

Expected: PASS.

---

### Task 2: Proposer CandidateBatch

**Files:**
- Modify: `gepa_researcher/proposer.py`
- Modify: `gepa_researcher/agent_components.py`
- Test: `tests/test_phase2_batch_flow.py`, `tests/test_agent_components.py`

- [ ] **Step 1: Write failing local proposer test**

Append to `Phase2BatchFlowTest`:

```python
    def test_rule_based_proposer_returns_default_batch_of_ten(self):
        from gepa_researcher.proposer import RuleBasedProposer
        from gepa_researcher.schemas import LoopState

        config = {
            "generation": {"batch_size": 10},
            "task": {"initial_prompt": "answer carefully"},
        }

        batch = RuleBasedProposer().propose_batch(LoopState(task_name="task"), config)

        self.assertEqual(batch.round_id, 0)
        self.assertEqual(len(batch.candidates), 10)
        self.assertEqual(batch.candidates[0].candidate_id, "cand_000_000")
        self.assertEqual(batch.candidates[-1].candidate_id, "cand_000_009")
        self.assertEqual(len({candidate.prompt_text for candidate in batch.candidates}), 10)
```

- [ ] **Step 2: Run local proposer test to verify it fails**

Run: `python -m unittest tests.test_phase2_batch_flow.Phase2BatchFlowTest.test_rule_based_proposer_returns_default_batch_of_ten -v`

Expected: FAIL with `AttributeError: 'RuleBasedProposer' object has no attribute 'propose_batch'`.

- [ ] **Step 3: Implement `RuleBasedProposer.propose_batch()`**

Add imports and method:

```python
from .schemas import Candidate, CandidateBatch, LoopState
```

```python
    def propose_batch(self, state: LoopState, config: dict[str, Any]) -> CandidateBatch:
        batch_size = int(config.get("generation", {}).get("batch_size", 10))
        candidates = []
        for index in range(batch_size):
            candidate = self.propose(state, config)
            candidate.candidate_id = f"cand_{state.round_id:03d}_{index:03d}"
            candidate.hypothesis = f"{candidate.hypothesis} Variant {index + 1}."
            candidate.proposed_change = f"{candidate.proposed_change} Batch variant {index + 1}."
            candidate.prompt_text = f"{candidate.prompt_text}\n\nBatch variant: {index + 1}"
            candidates.append(candidate)
        return CandidateBatch(round_id=state.round_id, candidates=candidates)
```

- [ ] **Step 4: Run local proposer test to verify it passes**

Run: `python -m unittest tests.test_phase2_batch_flow.Phase2BatchFlowTest.test_rule_based_proposer_returns_default_batch_of_ten -v`

Expected: PASS.

- [ ] **Step 5: Write failing agent proposer batch prompt test**

Append to `tests/test_agent_components.py`:

```python
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
```

- [ ] **Step 6: Run agent proposer test to verify it fails**

Run: `python -m unittest tests.test_agent_components.AgentComponentsTest.test_agent_proposer_requests_candidate_batch -v`

Expected: FAIL with missing `propose_batch`.

- [ ] **Step 7: Implement `AgentProposer.propose_batch()`**

Add `CandidateBatch` import. Implement a batch prompt mirroring the existing proposer constraints, but requiring:

```python
Required JSON schema:
{
  "candidates": [
    {
      "hypothesis": "short falsifiable model hypothesis",
      "target_module": "distribution_model",
      "proposed_change": "what to test this round",
      "rationale": "why this is a good next candidate",
      "expected_improvement": "what metric should improve",
      "risk": "main risk or failure mode",
      "model_family": "e.g. normal, lognormal, mixture, uniform, exponential, nonparametric",
      "analysis_plan": ["step 1", "step 2"]
    }
  ]
}
```

Convert each item into `Candidate` with IDs `cand_{state.round_id:03d}_{index:03d}` and return `CandidateBatch`.

- [ ] **Step 8: Run proposer tests**

Run: `python -m unittest tests.test_phase2_batch_flow.Phase2BatchFlowTest.test_rule_based_proposer_returns_default_batch_of_ten tests.test_agent_components.AgentComponentsTest.test_agent_proposer_requests_candidate_batch -v`

Expected: PASS.

---

### Task 3: ParallelExecutor With Failure Isolation And Archive

**Files:**
- Create: `gepa_researcher/parallel_executor.py`
- Modify: `gepa_researcher/agent_components.py`
- Test: `tests/test_phase2_batch_flow.py`

- [ ] **Step 1: Write failing parallel executor test**

Append to `tests/test_phase2_batch_flow.py`:

```python
import json
import tempfile
import time
from pathlib import Path

from gepa_researcher.schemas import SampleTrace


class RecordingExecutor:
    def __init__(self):
        self.workspace_by_candidate = {}

    def execute(self, candidate, config):
        workspace = Path(config["_candidate_workspace"])
        self.workspace_by_candidate[candidate.candidate_id] = workspace
        workspace.mkdir(parents=True, exist_ok=True)
        if candidate.candidate_id.endswith("_001"):
            raise RuntimeError("candidate failed intentionally")
        time.sleep(0.2)
        return Trace(
            candidate_id=candidate.candidate_id,
            round_id=candidate.round_id,
            samples=[
                SampleTrace(
                    sample_id="observed_numeric_dataset",
                    input="data.csv",
                    output="ok",
                    expected="unknown",
                    logs="ran",
                    artifacts={"workspace": str(workspace)},
                )
            ],
        )
```

Append method:

```python
    def test_parallel_executor_isolates_workspaces_and_records_failures(self):
        from gepa_researcher.parallel_executor import ParallelExecutor

        candidates = [self._candidate(f"cand_000_{index:03d}") for index in range(4)]
        batch = CandidateBatch(round_id=0, candidates=candidates)
        inner = RecordingExecutor()

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            trace_batch = ParallelExecutor(inner, run_dir).execute_batch(
                batch,
                {
                    "executor": {
                        "max_parallel_executors": 3,
                        "executor_timeout_seconds": 5,
                        "fail_fast": False,
                        "per_candidate_workspace": True,
                    }
                },
            )

            self.assertEqual(len(trace_batch.traces), 4)
            self.assertEqual(trace_batch.failed_candidate_ids, ["cand_000_001"])
            self.assertTrue((run_dir / "agent_work" / "round_000" / "cand_000_000").exists())
            self.assertTrue((run_dir / "agent_work" / "round_000" / "cand_000_002").exists())
            self.assertTrue((run_dir / "traces" / "round_000" / "cand_000_001" / "trace.json").exists())
            self.assertTrue((run_dir / "traces.jsonl").exists())

            failed_trace = next(trace for trace in trace_batch.traces if trace.candidate_id == "cand_000_001")
            self.assertIn("candidate failed intentionally", failed_trace.samples[0].error)

            jsonl_lines = (run_dir / "traces.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(jsonl_lines), 4)
            self.assertEqual(json.loads(jsonl_lines[0])["round_id"], 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_phase2_batch_flow.Phase2BatchFlowTest.test_parallel_executor_isolates_workspaces_and_records_failures -v`

Expected: FAIL with missing `gepa_researcher.parallel_executor`.

- [ ] **Step 3: Implement `ParallelExecutor`**

Create `gepa_researcher/parallel_executor.py`:

```python
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from time import perf_counter
from typing import Any

from .io_utils import append_jsonl, write_json
from .schemas import Candidate, CandidateBatch, SampleTrace, Trace, TraceBatch


class ParallelExecutor:
    def __init__(self, executor: Any, run_dir: Path):
        self.executor = executor
        self.run_dir = run_dir

    def execute_batch(self, batch: CandidateBatch, config: dict[str, Any]) -> TraceBatch:
        executor_config = config.get("executor", {})
        max_workers = int(executor_config.get("max_parallel_executors", 3))
        fail_fast = bool(executor_config.get("fail_fast", False))

        traces_by_id: dict[str, Trace] = {}
        failed_ids: list[str] = []

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(self._execute_one, candidate, config): candidate
                for candidate in batch.candidates
            }
            for future in as_completed(futures):
                candidate = futures[future]
                try:
                    trace = future.result()
                except Exception as exc:
                    trace = self._failure_trace(candidate, exc)
                traces_by_id[candidate.candidate_id] = trace
                if self._trace_failed(trace):
                    failed_ids.append(candidate.candidate_id)
                self._persist_trace(trace)
                if fail_fast and self._trace_failed(trace):
                    break

        traces = [traces_by_id[candidate.candidate_id] for candidate in batch.candidates if candidate.candidate_id in traces_by_id]
        return TraceBatch(round_id=batch.round_id, traces=traces, failed_candidate_ids=failed_ids)

    def _execute_one(self, candidate: Candidate, config: dict[str, Any]) -> Trace:
        candidate_config = dict(config)
        candidate_config["_candidate_workspace"] = str(self._workspace(candidate))
        candidate_config["_executor_timeout_seconds"] = int(
            config.get("executor", {}).get("executor_timeout_seconds", config.get("agent", {}).get("timeout_seconds", 600))
        )
        start = perf_counter()
        trace = self.executor.execute(candidate, candidate_config)
        trace.samples[0].artifacts.setdefault("executor_wall_seconds", round(perf_counter() - start, 4))
        return trace

    def _workspace(self, candidate: Candidate) -> Path:
        return self.run_dir / "agent_work" / f"round_{candidate.round_id:03d}" / candidate.candidate_id

    def _trace_path(self, trace: Trace) -> Path:
        return self.run_dir / "traces" / f"round_{trace.round_id:03d}" / trace.candidate_id / "trace.json"

    def _persist_trace(self, trace: Trace) -> None:
        write_json(self._trace_path(trace), trace.to_dict())
        append_jsonl(self.run_dir / "traces.jsonl", trace.to_dict())

    def _failure_trace(self, candidate: Candidate, exc: Exception) -> Trace:
        return Trace(
            candidate_id=candidate.candidate_id,
            round_id=candidate.round_id,
            samples=[
                SampleTrace(
                    sample_id="executor_failure",
                    input=candidate.prompt_text,
                    output="",
                    expected="executor completed",
                    logs="executor failed",
                    error=f"{type(exc).__name__}: {exc}",
                    artifacts={"workspace": str(self._workspace(candidate))},
                )
            ],
        )

    def _trace_failed(self, trace: Trace) -> bool:
        return any(sample.error for sample in trace.samples)
```

- [ ] **Step 4: Update `AgentExecutor.execute()` workspace**

In `gepa_researcher/agent_components.py`, change:

```python
round_dir = self.run_dir / "agent_work" / f"round_{candidate.round_id:03d}"
```

to:

```python
round_dir = Path(config.get("_candidate_workspace") or self.run_dir / "agent_work" / f"round_{candidate.round_id:03d}" / candidate.candidate_id)
```

- [ ] **Step 5: Run parallel executor test**

Run: `python -m unittest tests.test_phase2_batch_flow.Phase2BatchFlowTest.test_parallel_executor_isolates_workspaces_and_records_failures -v`

Expected: PASS.

---

### Task 4: BatchJudger

**Files:**
- Create: `gepa_researcher/batch_judger.py`
- Test: `tests/test_phase2_batch_flow.py`

- [ ] **Step 1: Write failing batch judger test**

Append:

```python
class ScoreByCandidateJudger:
    def judge(self, candidate, trace, config):
        score = 0.9 if candidate.candidate_id.endswith("_000") else 0.2
        return Judgment(
            candidate_id=candidate.candidate_id,
            round_id=candidate.round_id,
            score=score,
            passed=score >= 0.85,
            per_sample_scores=[],
            failure_categories=[] if score >= 0.85 else ["weak_fit"],
            actionable_feedback=["keep simple"] if score >= 0.85 else ["improve fit"],
            confidence="high",
        )
```

Append method:

```python
    def test_batch_judger_emits_per_candidate_judgments_and_summary(self):
        from gepa_researcher.batch_judger import BatchJudger

        candidates = [self._candidate(f"cand_000_{index:03d}") for index in range(3)]
        candidate_batch = CandidateBatch(round_id=0, candidates=candidates)
        trace_batch = TraceBatch(
            round_id=0,
            traces=[Trace(candidate_id=candidate.candidate_id, round_id=0, samples=[]) for candidate in candidates],
            failed_candidate_ids=[],
        )

        judgment_batch = BatchJudger(ScoreByCandidateJudger()).judge_batch(candidate_batch, trace_batch, {})

        self.assertEqual(len(judgment_batch.judgments), 3)
        self.assertEqual(judgment_batch.summary["candidate_count"], 3)
        self.assertEqual(judgment_batch.summary["best_candidate_id"], "cand_000_000")
        self.assertEqual(judgment_batch.summary["failure_categories"], {"weak_fit": 2})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_phase2_batch_flow.Phase2BatchFlowTest.test_batch_judger_emits_per_candidate_judgments_and_summary -v`

Expected: FAIL with missing `batch_judger`.

- [ ] **Step 3: Implement `BatchJudger`**

Create `gepa_researcher/batch_judger.py`:

```python
from __future__ import annotations

from collections import Counter
from typing import Any

from .schemas import CandidateBatch, Judgment, JudgmentBatch, TraceBatch


class BatchJudger:
    def __init__(self, judger: Any):
        self.judger = judger

    def judge_batch(self, candidate_batch: CandidateBatch, trace_batch: TraceBatch, config: dict[str, Any]) -> JudgmentBatch:
        trace_by_id = {trace.candidate_id: trace for trace in trace_batch.traces}
        judgments: list[Judgment] = []
        for candidate in candidate_batch.candidates:
            trace = trace_by_id[candidate.candidate_id]
            judgments.append(self.judger.judge(candidate, trace, config))

        best = max(judgments, key=lambda judgment: judgment.score, default=None)
        failures = Counter()
        for judgment in judgments:
            failures.update(judgment.failure_categories)

        summary = {
            "candidate_count": len(judgments),
            "best_candidate_id": best.candidate_id if best else None,
            "best_score": best.score if best else None,
            "failed_candidate_ids": list(trace_batch.failed_candidate_ids),
            "failure_categories": dict(failures),
        }
        return JudgmentBatch(round_id=candidate_batch.round_id, judgments=judgments, summary=summary)
```

- [ ] **Step 4: Run batch judger test**

Run: `python -m unittest tests.test_phase2_batch_flow.Phase2BatchFlowTest.test_batch_judger_emits_per_candidate_judgments_and_summary -v`

Expected: PASS.

---

### Task 5: TopKGater

**Files:**
- Create: `gepa_researcher/topk_gater.py`
- Test: `tests/test_phase2_batch_flow.py`

- [ ] **Step 1: Write failing top-k gate test**

Append:

```python
    def test_topk_gater_keeps_best_candidates_and_feedback(self):
        from gepa_researcher.schemas import LoopState
        from gepa_researcher.topk_gater import TopKGater

        judgments = [
            Judgment(
                candidate_id=f"cand_000_{index:03d}",
                round_id=0,
                score=score,
                passed=False,
                per_sample_scores=[],
                failure_categories=["weak_fit"] if score < 0.8 else [],
                actionable_feedback=[f"feedback {index}"],
                confidence="medium",
            )
            for index, score in enumerate([0.1, 0.95, 0.7, 0.4])
        ]
        judgment_batch = JudgmentBatch(round_id=0, judgments=judgments, summary={})

        decision = TopKGater().decide_generation(
            LoopState(task_name="task"),
            judgment_batch,
            {"generation": {"top_k": 2}, "judger": {"pass_threshold": 0.99}, "budget": {"max_rounds": 3, "no_improvement_patience": 2}},
        )

        self.assertEqual(decision.kept, ["cand_000_001", "cand_000_002"])
        self.assertEqual(decision.rejected, ["cand_000_000", "cand_000_003"])
        self.assertFalse(decision.stop)
        self.assertIn("feedback 1", decision.next_feedback)
        self.assertIn("Common failure weak_fit appeared in 3 candidate(s).", decision.next_feedback)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_phase2_batch_flow.Phase2BatchFlowTest.test_topk_gater_keeps_best_candidates_and_feedback -v`

Expected: FAIL with missing `topk_gater`.

- [ ] **Step 3: Implement `TopKGater`**

Create `gepa_researcher/topk_gater.py`:

```python
from __future__ import annotations

from collections import Counter
from typing import Any

from .schemas import GenerationDecision, JudgmentBatch, LoopState


class TopKGater:
    def decide_generation(self, state: LoopState, judgment_batch: JudgmentBatch, config: dict[str, Any]) -> GenerationDecision:
        top_k = int(config.get("generation", {}).get("top_k", 3))
        pass_threshold = float(config.get("judger", {}).get("pass_threshold", 1.0))
        max_rounds = int(config.get("budget", {}).get("max_rounds", state.round_id + 1))
        patience = int(config.get("budget", {}).get("no_improvement_patience", 999999))

        ordered = sorted(judgment_batch.judgments, key=lambda judgment: judgment.score, reverse=True)
        kept = [judgment.candidate_id for judgment in ordered[:top_k]]
        rejected = [judgment.candidate_id for judgment in ordered[top_k:]]
        best = ordered[0] if ordered else None

        failures = Counter()
        next_feedback: list[str] = []
        for judgment in ordered[:top_k]:
            next_feedback.extend(judgment.actionable_feedback)
        for judgment in judgment_batch.judgments:
            failures.update(judgment.failure_categories)
        for failure, count in failures.most_common():
            next_feedback.append(f"Common failure {failure} appeared in {count} candidate(s).")

        improved = best is not None and best.score > state.best_score
        no_improvement = 0 if improved else state.no_improvement_rounds + 1
        stop = False
        if best and best.score >= pass_threshold:
            stop = True
        elif state.round_id + 1 >= max_rounds:
            stop = True
        elif no_improvement >= patience:
            stop = True

        return GenerationDecision(
            round_id=judgment_batch.round_id,
            kept=kept,
            rejected=rejected,
            next_feedback=list(dict.fromkeys(next_feedback)),
            stop=stop,
            artifacts={
                "best_candidate_id": best.candidate_id if best else None,
                "best_score": best.score if best else None,
                "top_k": top_k,
                "failure_categories": dict(failures),
            },
        )
```

- [ ] **Step 4: Run top-k gate test**

Run: `python -m unittest tests.test_phase2_batch_flow.Phase2BatchFlowTest.test_topk_gater_keeps_best_candidates_and_feedback -v`

Expected: PASS.

---

### Task 6: Orchestrator Batch Generation And Archive

**Files:**
- Modify: `gepa_researcher/orchestrator.py`
- Test: `tests/test_phase2_batch_flow.py`, `tests/test_smoke.py`

- [ ] **Step 1: Write failing orchestrator generation archive test**

Append:

```python
    def test_orchestrator_run_generation_archives_all_phase2_outputs(self):
        from gepa_researcher.orchestrator import ResearchOrchestrator
        from gepa_researcher.io_utils import read_json
        from gepa_researcher.schemas import LoopState

        config_path = Path("examples/paper_qa/config.json").resolve()
        config = read_json(config_path)
        config["generation"] = {"batch_size": 10, "top_k": 3}
        config["executor"] = {
            "max_parallel_executors": 3,
            "executor_timeout_seconds": 5,
            "fail_fast": False,
            "per_candidate_workspace": True,
        }

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            config["run_dir"] = str(run_dir)
            orchestrator = ResearchOrchestrator(config=config, config_path=config_path)
            decision = orchestrator.run_generation(0, LoopState(task_name="paper_qa"))

            self.assertEqual(len(decision.kept), 3)
            self.assertTrue((run_dir / "candidates.jsonl").exists())
            self.assertTrue((run_dir / "traces.jsonl").exists())
            self.assertTrue((run_dir / "judgments.jsonl").exists())
            self.assertTrue((run_dir / "decisions.jsonl").exists())
            self.assertTrue((run_dir / "live" / "round_000_candidate_batch.json").exists())
            self.assertTrue((run_dir / "live" / "round_000_trace_batch.json").exists())
            self.assertTrue((run_dir / "live" / "round_000_judgment_batch.json").exists())
            self.assertTrue((run_dir / "live" / "round_000_generation_decision.json").exists())

            candidate_lines = (run_dir / "candidates.jsonl").read_text(encoding="utf-8").splitlines()
            judgment_lines = (run_dir / "judgments.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(candidate_lines), 10)
            self.assertEqual(len(judgment_lines), 10)
```

- [ ] **Step 2: Run orchestrator archive test to verify it fails**

Run: `python -m unittest tests.test_phase2_batch_flow.Phase2BatchFlowTest.test_orchestrator_run_generation_archives_all_phase2_outputs -v`

Expected: FAIL with missing `run_generation`.

- [ ] **Step 3: Update imports and component build**

In `gepa_researcher/orchestrator.py`, import:

```python
from .batch_judger import BatchJudger
from .parallel_executor import ParallelExecutor
from .schemas import Candidate, CandidateBatch, Decision, GenerationDecision, Judgment, JudgmentBatch, LoopState, Trace, TraceBatch
from .topk_gater import TopKGater
```

In `_build_components()`, keep returning single-candidate components. The orchestrator wraps them at generation time.

- [ ] **Step 4: Implement `run_generation()`**

Add:

```python
    def run_generation(self, round_id: int, state: LoopState) -> GenerationDecision:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        state.round_id = round_id

        self._log("proposer batch started")
        if hasattr(self.proposer, "propose_batch"):
            candidate_batch = self.proposer.propose_batch(state, self.config)
        else:
            candidate_batch = CandidateBatch(round_id=round_id, candidates=[self.proposer.propose(state, self.config)])
        self._write_live_artifact(round_id, "candidate_batch", candidate_batch.to_dict())
        self._persist_candidate_batch(candidate_batch)
        self._log(f"proposer batch finished: {len(candidate_batch.candidates)} candidate(s)")

        self._log("parallel executor started")
        trace_batch = ParallelExecutor(self.executor, self.run_dir).execute_batch(candidate_batch, self.config)
        self._write_live_artifact(round_id, "trace_batch", trace_batch.to_dict())
        self._log(f"parallel executor finished: {len(trace_batch.traces)} trace(s), failures={len(trace_batch.failed_candidate_ids)}")

        self._log("batch judger started")
        judgment_batch = BatchJudger(self.judger).judge_batch(candidate_batch, trace_batch, self.config)
        self._write_live_artifact(round_id, "judgment_batch", judgment_batch.to_dict())
        self._persist_judgment_batch(judgment_batch)
        self._log(f"batch judger finished: best={judgment_batch.summary.get('best_candidate_id')}")

        self._log("top-k gater started")
        decision = TopKGater().decide_generation(state, judgment_batch, self.config)
        self._write_live_artifact(round_id, "generation_decision", decision.to_dict())
        self._persist_generation_decision(decision)
        self._log(f"top-k gater finished: kept={decision.kept}, stop={decision.stop}")
        return decision
```

- [ ] **Step 5: Implement batch persistence helpers**

Add:

```python
    def _persist_candidate_batch(self, batch: CandidateBatch) -> None:
        round_dir = self.run_dir / "traces" / f"round_{batch.round_id:03d}"
        write_json(round_dir / "candidate_batch.json", batch.to_dict())
        for candidate in batch.candidates:
            write_json(round_dir / candidate.candidate_id / "candidate.json", candidate.to_dict())
            append_jsonl(self.run_dir / "candidates.jsonl", candidate.to_dict())

    def _persist_judgment_batch(self, batch: JudgmentBatch) -> None:
        round_dir = self.run_dir / "traces" / f"round_{batch.round_id:03d}"
        write_json(round_dir / "judgment_batch.json", batch.to_dict())
        for judgment in batch.judgments:
            write_json(round_dir / judgment.candidate_id / "judgment.json", judgment.to_dict())
            append_jsonl(self.run_dir / "judgments.jsonl", judgment.to_dict())

    def _persist_generation_decision(self, decision: GenerationDecision) -> None:
        round_dir = self.run_dir / "traces" / f"round_{decision.round_id:03d}"
        write_json(round_dir / "generation_decision.json", decision.to_dict())
        append_jsonl(self.run_dir / "decisions.jsonl", decision.to_dict())
```

- [ ] **Step 6: Update `run()` and state update**

Replace the single-candidate body inside the round loop with:

```python
            decision = self.run_generation(round_id, state)
            self._update_state_from_generation(state, decision)
            write_json(self.run_dir / "state.json", state.to_dict())
            self._log(f"Round {round_id + 1}/{max_rounds} persisted")

            if decision.stop:
                self._log(f"Stopping after round {round_id + 1}")
                break
```

Add:

```python
    def _update_state_from_generation(self, state: LoopState, decision: GenerationDecision) -> None:
        best_score = decision.artifacts.get("best_score")
        best_candidate_id = decision.artifacts.get("best_candidate_id")
        improved = best_score is not None and float(best_score) > state.best_score
        if improved:
            state.best_score = float(best_score)
            state.best_candidate_id = str(best_candidate_id)
            state.no_improvement_rounds = 0
        else:
            state.no_improvement_rounds += 1

        state.history.append(
            {
                "round_id": decision.round_id,
                "kept": decision.kept,
                "rejected": decision.rejected,
                "best_candidate_id": best_candidate_id,
                "best_score": best_score,
                "next_feedback": decision.next_feedback,
                "stop": decision.stop,
            }
        )
        state.round_id = decision.round_id + 1
```

Update `_write_final_report()` to handle both old single-candidate history keys and new generation keys.

- [ ] **Step 7: Run orchestrator archive test**

Run: `python -m unittest tests.test_phase2_batch_flow.Phase2BatchFlowTest.test_orchestrator_run_generation_archives_all_phase2_outputs -v`

Expected: PASS.

- [ ] **Step 8: Run existing smoke tests and update expectations only if needed**

Run: `python -m unittest tests.test_smoke -v`

Expected: PASS after adapting progress-text assertions to include batch labels such as `proposer batch started`, `parallel executor started`, `batch judger started`, and `top-k gater started`.

---

### Task 7: Config Defaults For Function Discovery

**Files:**
- Modify: `examples/function_discovery/config.claude.json`
- Test: `tests/test_smoke.py`

- [ ] **Step 1: Write failing config test**

Extend `test_claude_config_uses_conda_myenv_runtime` in `tests/test_smoke.py`:

```python
        self.assertEqual(config["generation"]["batch_size"], 10)
        self.assertEqual(config["generation"]["top_k"], 3)
        self.assertGreaterEqual(config["executor"]["max_parallel_executors"], 3)
        self.assertEqual(config["executor"]["executor_timeout_seconds"], 900)
        self.assertFalse(config["executor"]["fail_fast"])
        self.assertTrue(config["executor"]["per_candidate_workspace"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_smoke.OrchestratorSmokeTest.test_claude_config_uses_conda_myenv_runtime -v`

Expected: FAIL with missing `generation` or `executor`.

- [ ] **Step 3: Update function discovery config**

Add:

```json
  "generation": {
    "batch_size": 10,
    "top_k": 3
  },
  "executor": {
    "max_parallel_executors": 3,
    "executor_timeout_seconds": 900,
    "fail_fast": false,
    "per_candidate_workspace": true
  },
```

- [ ] **Step 4: Run config test**

Run: `python -m unittest tests.test_smoke.OrchestratorSmokeTest.test_claude_config_uses_conda_myenv_runtime -v`

Expected: PASS.

---

### Task 8: Full Verification Against MVP Acceptance Criteria

**Files:**
- Test: all tests
- Optional manual run: `examples/function_discovery/config.claude.json`

- [ ] **Step 1: Run full unit suite**

Run: `python -m unittest discover -v`

Expected: all tests PASS.

- [ ] **Step 2: Verify archive files in a local mock run**

Run: `python -m unittest tests.test_phase2_batch_flow.Phase2BatchFlowTest.test_orchestrator_run_generation_archives_all_phase2_outputs -v`

Expected: PASS and test checks:
- 10 candidate JSONL lines
- 10 judgment JSONL lines
- `traces.jsonl`
- live batch artifacts
- per-candidate trace directories

- [ ] **Step 3: Optional real agent run for function discovery**

Run only after unit tests pass and when the Claude Code agent is available:

`python -m gepa_researcher.orchestrator --config examples/function_discovery/config.claude.json`

Expected:
- Proposer returns at least 10 candidates in round 0.
- Executor runs with `max_parallel_executors >= 3`.
- Each candidate writes under `agent_work/round_000/cand_000_YYY`.
- Each trace writes under `traces/round_000/cand_000_YYY/trace.json`.
- `traces.jsonl`, `candidates.jsonl`, `judgments.jsonl`, and `decisions.jsonl` contain all attempted candidates and the generation decision.

- [ ] **Step 4: Acceptance checklist**

Confirm with fresh evidence:
- MVP 1: `CandidateBatch` contains at least 10 structured candidates.
- MVP 2: `ParallelExecutor` honors `max_parallel_executors >= 3`.
- MVP 3: A failed candidate does not fail the generation and records the error.
- MVP 4: `BatchJudger` emits independent judgments and summary.
- MVP 5: `TopKGater` emits top-k kept candidates and next feedback.
- MVP 6: Archive keeps all candidates, traces, judgments, and decisions.

