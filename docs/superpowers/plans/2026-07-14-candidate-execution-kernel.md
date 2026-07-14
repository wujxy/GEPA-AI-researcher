# Candidate Execution Kernel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the old candidate-owned workspace lifecycle with a v1.0.5 Candidate Execution Kernel where CandidateCard is persistent business state, Revision is immutable code state, Execution is one concrete task, and Sandbox is temporary infrastructure.

**Architecture:** Introduce focused domain objects and append-only stores first, then route execution through immutable ExecutionSpec records and per-execution sandboxes. Reuse existing agent JSON contracts, runtime launch logic, commit audit, scoring, gate, and Pareto logic while deleting candidate-keyed workspace/execution reuse.

**Tech Stack:** Python 3.10+, dataclasses, JSON/JSONL filesystem stores, unittest/pytest-compatible tests, Git CLI, existing local/apptainer runtime backend code.

## Global Constraints

- Do not preserve legacy `materialize_once`; feedback and pareto evaluations must materialize from `CandidateCard.result_revision`.
- Do not keep `WorkspaceLease` as persistent business state.
- Do not store `workspace_path`, `worktree_path`, `branch_name`, `container_home`, process IDs, or scratch paths in `CandidateCard`.
- `ExecutionStore` is keyed by `execution_id` and keeps complete history.
- One Execution creates one Sandbox and may delete it after collecting records and artifacts.
- Parent optimization lineage and code inheritance are separate: `parent_candidate_ids` may contain many IDs, but `base_revision` is one commit SHA.
- Agent code may receive temporary repo/artifact paths at runtime, but persistent stores must use revisions, execution IDs, and artifact refs as authoritative identity.
- No compatibility layer for old `execution_registry.json` is required.

---

## File Structure

- Create `gepa_researcher/domain/candidate.py`: `CandidateStatus`, `ProposalIdea`, `CandidateCard`, conversion from legacy proposer `Candidate`.
- Create `gepa_researcher/domain/execution.py`: `ExecutionPhase`, `ExecutionStatus`, `ExecutionBudget`, `CapabilityPolicy`, `ExecutionSpec`, `ExecutionRecord`, typed failure.
- Create `gepa_researcher/domain/revision.py`: `RevisionRef` and SHA validation helpers.
- Create `gepa_researcher/domain/artifact.py`: `ArtifactKind`, `ArtifactRef`.
- Create `gepa_researcher/storage/candidate_store.py`: JSON candidate card persistence and round/children queries.
- Create `gepa_researcher/storage/execution_store.py`: append-only execution persistence keyed by execution ID.
- Create `gepa_researcher/storage/artifact_store.py`: artifact root management and relative artifact refs.
- Create `gepa_researcher/execution/materializer.py`: materialize an input revision into a per-execution sandbox repo.
- Create `gepa_researcher/execution/sandbox.py`: `SandboxSession` and sandbox cleanup helpers.
- Create `gepa_researcher/execution/git_result.py`: finalization of implementation executions using existing `audit_commit`.
- Create `gepa_researcher/services/candidate_factory.py`: CandidateCard creation from proposal plus selected code-base parent.
- Create `gepa_researcher/services/candidate_scheduler.py`: CandidateCard status to next ExecutionSpec.
- Create `gepa_researcher/services/execution_service.py`: execute one spec through materializer, runtime backend, runner, audit, artifact collection, and stores.
- Modify `gepa_researcher/models/schemas.py`: keep existing trace/judgment/score models; remove or stop using persistent `WorkspaceLease` and old `ExecutionRecord` after migration.
- Modify `gepa_researcher/execution/runtime_backend.py`: accept sandbox/session objects instead of `WorkspaceLease`.
- Modify `gepa_researcher/agents/adapters.py`: shrink to runner/judger adapters; remove workspace preparation and registry writes.
- Modify `gepa_researcher/agents/agent_components.py`: replace `_execution_mode` with explicit phase/capability context.
- Modify `gepa_researcher/orchestrator.py`: orchestrate CandidateCard, scheduler, execution service, and stores.
- Modify `gepa_researcher/config/resolver.py`: stop emitting `execution.lifecycle`; keep runtime/isolation/safety contracts.
- Delete `gepa_researcher/storage/registry.py` after all call sites are migrated.
- Rewrite tests that protect old lifecycle assumptions; preserve tests for admission, audit, frozen path rejection, runtime env redaction, score/gate/pareto.

---

### Task 1: Domain Objects And Store Contracts

**Files:**
- Create: `gepa_researcher/domain/__init__.py`
- Create: `gepa_researcher/domain/candidate.py`
- Create: `gepa_researcher/domain/execution.py`
- Create: `gepa_researcher/domain/revision.py`
- Create: `gepa_researcher/domain/artifact.py`
- Create: `tests/test_candidate_execution_domain.py`

**Interfaces:**
- Consumes: existing proposer `gepa_researcher.models.schemas.Candidate` only as legacy proposal payload.
- Produces:
  - `ProposalIdea.from_candidate(candidate: Candidate) -> ProposalIdea`
  - `CandidateCard.to_dict() -> dict[str, Any]`
  - `CandidateCard.from_dict(data: dict[str, Any]) -> CandidateCard`
  - `ExecutionSpec.to_dict() -> dict[str, Any]`
  - `ExecutionRecord.to_dict() -> dict[str, Any]`
  - `RevisionRef.validate_sha(value: str) -> str`

- [ ] **Step 1: Write failing domain serialization tests**

Add `tests/test_candidate_execution_domain.py` with:

```python
from __future__ import annotations

import unittest

from gepa_researcher.domain.candidate import CandidateCard, CandidateStatus, ProposalIdea
from gepa_researcher.domain.execution import (
    CapabilityPolicy,
    ExecutionBudget,
    ExecutionPhase,
    ExecutionRecord,
    ExecutionSpec,
    ExecutionStatus,
)
from gepa_researcher.domain.revision import RevisionRef


class CandidateExecutionDomainTest(unittest.TestCase):
    def test_candidate_card_serializes_without_workspace_identity(self):
        proposal = ProposalIdea(
            proposal_id="proposal-1",
            hypothesis="inline hot function",
            scope="src/hot.cc",
            proposed_change="replace call with inline body",
            rationale="reduce call overhead",
            expected_improvement="lower latency",
            risk="low",
            prompt_text="Strategy: inline",
            target_files=("src/hot.cc",),
            executor_contract={"instructions": "edit src/hot.cc and run benchmark"},
            expected_artifacts=("benchmark.json",),
            metadata={"strategy": "safe-pattern #1"},
        )
        card = CandidateCard(
            candidate_id="cand_001_000",
            round_id=1,
            parent_candidate_ids=("seed_000",),
            proposal_id=proposal.proposal_id,
            proposal=proposal,
            base_revision="a" * 40,
            status=CandidateStatus.ADMITTED,
        )

        data = card.to_dict()

        self.assertEqual(data["base_revision"], "a" * 40)
        self.assertEqual(data["execution_ids"], [])
        self.assertNotIn("workspace_path", str(data))
        self.assertNotIn("worktree_path", str(data))
        self.assertEqual(CandidateCard.from_dict(data).proposal.hypothesis, "inline hot function")

    def test_execution_spec_and_record_are_execution_id_keyed(self):
        spec = ExecutionSpec(
            execution_id="exec-001",
            run_id="run-001",
            round_id=1,
            candidate_id="cand_001_000",
            phase=ExecutionPhase.IMPLEMENTATION,
            input_revision="a" * 40,
            dataset_ref=None,
            evaluator_version=None,
            budget=ExecutionBudget(wall_seconds=600, max_tokens=None, max_files_changed=3, max_commands=None),
            capability_policy=CapabilityPolicy(
                repo_writable=True,
                network_allowed=False,
                allowed_tools=("bash", "git"),
                forbidden_paths=("tests/**",),
            ),
        )
        record = ExecutionRecord.from_spec(spec)

        self.assertEqual(record.execution_id, "exec-001")
        self.assertEqual(record.status, ExecutionStatus.PENDING)
        self.assertEqual(record.input_revision, "a" * 40)
        self.assertEqual(record.phase, ExecutionPhase.IMPLEMENTATION)

    def test_revision_ref_rejects_non_sha_values(self):
        self.assertEqual(RevisionRef.validate_sha("b" * 40), "b" * 40)
        with self.assertRaises(ValueError):
            RevisionRef.validate_sha("/tmp/worktree")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_candidate_execution_domain.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'gepa_researcher.domain'`.

- [ ] **Step 3: Implement domain objects**

Implement the files listed above. Required field rules:

```python
class CandidateStatus(str, Enum):
    GENERATED = "generated"
    ADMITTED = "admitted"
    MATERIALIZING = "materializing"
    MATERIALIZED = "materialized"
    EVALUATING = "evaluating"
    EVALUATED = "evaluated"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    IMPLEMENTATION_FAILED = "implementation_failed"
    EVALUATION_FAILED = "evaluation_failed"
    CANCELLED = "cancelled"
```

`CandidateCard` fields must include `candidate_id`, `round_id`, `parent_candidate_ids`, `proposal_id`, `proposal`, `base_revision`, `status`, `result_revision`, `execution_ids`, `judgment_ids`, `artifact_ids`, `final_decision`, `score_summary`, `created_at`, and `updated_at`.

`ExecutionPhase` must include `IMPLEMENTATION`, `FEEDBACK_EVAL`, `PARETO_EVAL`, `ROBUSTNESS_EVAL`, and `REPAIR`.

`ExecutionRecord.from_spec()` must create a pending record with `result_revision=None`, empty metrics, empty artifact refs, and no failure.

- [ ] **Step 4: Run domain tests**

Run: `python -m pytest tests/test_candidate_execution_domain.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gepa_researcher/domain tests/test_candidate_execution_domain.py
git commit -m "feat: add candidate execution domain objects"
```

---

### Task 2: Append-Only Candidate, Execution, And Artifact Stores

**Files:**
- Create: `gepa_researcher/storage/candidate_store.py`
- Create: `gepa_researcher/storage/execution_store.py`
- Create: `gepa_researcher/storage/artifact_store.py`
- Create: `tests/test_candidate_execution_stores.py`

**Interfaces:**
- Consumes: domain objects from Task 1.
- Produces:
  - `CandidateStore.save(card: CandidateCard) -> None`
  - `CandidateStore.get(candidate_id: str) -> CandidateCard | None`
  - `CandidateStore.list_by_round(round_id: int) -> list[CandidateCard]`
  - `CandidateStore.list_children(parent_candidate_id: str) -> list[CandidateCard]`
  - `ExecutionStore.create_pending(spec: ExecutionSpec) -> ExecutionRecord`
  - `ExecutionStore.save(record: ExecutionRecord) -> None`
  - `ExecutionStore.get(execution_id: str) -> ExecutionRecord | None`
  - `ExecutionStore.list_for_candidate(candidate_id: str) -> list[ExecutionRecord]`
  - `ExecutionStore.list_active() -> list[ExecutionRecord]`
  - `ExecutionStore.list_by_phase(candidate_id: str, phase: ExecutionPhase) -> list[ExecutionRecord]`
  - `ArtifactStore.put(execution_id: str, kind: ArtifactKind, file_path: Path) -> ArtifactRef`

- [ ] **Step 1: Write failing store tests**

Add `tests/test_candidate_execution_stores.py` with tests that:

```python
def test_execution_store_keeps_multiple_records_for_one_candidate(tmp_path):
    store = ExecutionStore(tmp_path)
    spec_a = _spec("exec-a", ExecutionPhase.IMPLEMENTATION)
    spec_b = _spec("exec-b", ExecutionPhase.FEEDBACK_EVAL)

    record_a = store.create_pending(spec_a)
    record_b = store.create_pending(spec_b)

    assert store.get("exec-a").execution_id == "exec-a"
    assert [r.execution_id for r in store.list_for_candidate("cand_001_000")] == ["exec-a", "exec-b"]
    assert [r.execution_id for r in store.list_by_phase("cand_001_000", ExecutionPhase.FEEDBACK_EVAL)] == ["exec-b"]
```

Also test that `CandidateStore.list_children("seed_000")` returns children whose `parent_candidate_ids` contains `seed_000`, and that `ArtifactStore.put()` copies or indexes files under `artifacts/<execution_id>/`.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_candidate_execution_stores.py -q`

Expected: FAIL because store modules do not exist.

- [ ] **Step 3: Implement stores using JSON and JSONL**

Persist candidates as:

```text
<run_dir>/candidates/<candidate_id>.json
<run_dir>/candidates.jsonl
```

Persist executions as:

```text
<run_dir>/executions/<execution_id>.json
<run_dir>/executions.jsonl
```

Persist artifact refs as:

```text
<run_dir>/artifacts/<execution_id>/<filename>
<run_dir>/artifacts.jsonl
```

`ExecutionStore.save()` must overwrite only `executions/<execution_id>.json` and append the latest state to JSONL; it must never index by candidate ID.

- [ ] **Step 4: Run store tests**

Run: `python -m pytest tests/test_candidate_execution_stores.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gepa_researcher/storage/candidate_store.py gepa_researcher/storage/execution_store.py gepa_researcher/storage/artifact_store.py tests/test_candidate_execution_stores.py
git commit -m "feat: add candidate execution stores"
```

---

### Task 3: Per-Execution Materializer And Sandbox Session

**Files:**
- Create: `gepa_researcher/execution/sandbox.py`
- Create: `gepa_researcher/execution/materializer.py`
- Modify: `gepa_researcher/execution/workspace.py`
- Create: `tests/test_execution_materializer.py`

**Interfaces:**
- Consumes: `ExecutionSpec.input_revision`, workspace config, and existing `_git` helpers.
- Produces:
  - `SandboxSession(execution_id, repo_path, artifact_path, scratch_path, input_revision, mode, temporary_paths)`
  - `RepositoryMaterializer.materialize(spec: ExecutionSpec) -> SandboxSession`
  - `RepositoryMaterializer.cleanup(session: SandboxSession) -> None`

- [ ] **Step 1: Write failing materializer tests**

Add `tests/test_execution_materializer.py` with:

```python
def test_materializer_uses_execution_id_not_candidate_id(tmp_path):
    repo, baseline = make_repo(tmp_path)
    spec_a = make_spec("exec-a", "cand_same", baseline)
    spec_b = make_spec("exec-b", "cand_same", baseline)
    materializer = RepositoryMaterializer(
        run_dir=tmp_path / "run",
        workspace_config={
            "mode": "git_worktree",
            "repo_path": str(repo),
            "baseline_ref": baseline,
            "root": str(tmp_path / "run" / "sandboxes"),
            "branch_prefix": "gepa/test",
        },
    )

    session_a = materializer.materialize(spec_a)
    session_b = materializer.materialize(spec_b)

    assert session_a.repo_path != session_b.repo_path
    assert "exec-a" in str(session_a.repo_path)
    assert "exec-b" in str(session_b.repo_path)
    assert session_a.input_revision == baseline
    assert session_b.input_revision == baseline
```

Add a second test that pre-materialized LFS paths are copied into each execution sandbox and allowed dirty paths are attributed before running.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_execution_materializer.py -q`

Expected: FAIL because `RepositoryMaterializer` does not exist.

- [ ] **Step 3: Implement materializer**

Move reusable logic from `WorkspaceManager.prepare()` into `RepositoryMaterializer.materialize()`:

- Resolve `spec.input_revision` using `git rev-parse --verify`.
- Create repo at `<root>/<execution_id>/repo`.
- Create artifacts at `<root>/<execution_id>/artifacts`.
- Create scratch at `<root>/<execution_id>/scratch`.
- Use `git worktree add -B <branch> <repo> <input_revision>` for the first implementation.
- Keep `GIT_LFS_SKIP_SMUDGE=1`, `GIT_TERMINAL_PROMPT=0`, and filter config currently used by `WorkspaceManager`.
- Run existing pre-materialized LFS copy logic.
- Return `SandboxSession`.

Branch name may use execution ID:

```text
<branch_prefix>/exec/<execution_id>
```

- [ ] **Step 4: Run materializer tests and existing workspace safety tests that remain relevant**

Run:

```bash
python -m pytest tests/test_execution_materializer.py -q
python -m pytest tests/test_p0_safety.py::WorkspaceAndProvenanceTest::test_audit_commit_records_changed_files_and_ignores_runtime_debris -q
python -m pytest tests/test_p0_safety.py::WorkspaceAndProvenanceTest::test_audit_commit_flags_frozen_path_edits -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gepa_researcher/execution/sandbox.py gepa_researcher/execution/materializer.py gepa_researcher/execution/workspace.py tests/test_execution_materializer.py
git commit -m "feat: materialize per-execution sandboxes"
```

---

### Task 4: Runtime Backend Uses SandboxSession

**Files:**
- Modify: `gepa_researcher/execution/runtime_backend.py`
- Modify: `tests/test_runtime_backend.py`

**Interfaces:**
- Consumes: `SandboxSession`, `ExecutionSpec`, `ExecutionRecord`.
- Produces:
  - `runtime_backend_for(config, run_dir).prepare(spec, session, record) -> RuntimeLease`

- [ ] **Step 1: Rewrite failing runtime backend tests**

Update `tests/test_runtime_backend.py` helpers:

```python
def _session(root: Path):
    repo = root / "repo"
    artifacts = root / "artifacts"
    scratch = root / "scratch"
    repo.mkdir()
    artifacts.mkdir()
    scratch.mkdir()
    return SandboxSession(
        execution_id="exec-1",
        repo_path=repo,
        artifact_path=artifacts,
        scratch_path=scratch,
        input_revision="a" * 40,
        mode="git_worktree",
        temporary_paths=(repo, artifacts, scratch),
    )
```

Change calls from:

```python
runtime_backend_for({"executor": {}}, root).prepare(_candidate(), lease, _record())
```

to:

```python
runtime_backend_for({"executor": {}}, root).prepare(_spec(), session, record)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_runtime_backend.py -q`

Expected: FAIL because runtime backend still expects `Candidate` and `WorkspaceLease`.

- [ ] **Step 3: Update runtime backend signatures**

Refactor both backend classes:

- `LocalRuntimeBackend.prepare(spec, session, record)` uses `session.repo_path` and `session.artifact_path`.
- `ApptainerRuntimeBackend.prepare(spec, session, record)` uses `session.repo_path`, `session.artifact_path`, and `session.scratch_path`.
- Env keys become:
  - `GEPA_CANDIDATE_ID=spec.candidate_id`
  - `GEPA_EXECUTION_ID=spec.execution_id`
  - `GEPA_INPUT_REVISION=spec.input_revision`
  - `GEPA_WORKTREE=<repo path visible to runner>`
  - `GEPA_ARTIFACTS=<artifact path visible to runner>`

Do not bind git common dir unless `session.mode == "git_worktree"` and the materializer still produces linked worktrees. Prefer removing `_git_common_dir_for_worktree()` once the materializer uses independent clones.

- [ ] **Step 4: Run runtime backend tests**

Run: `python -m pytest tests/test_runtime_backend.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gepa_researcher/execution/runtime_backend.py tests/test_runtime_backend.py
git commit -m "refactor: prepare runtime backends from sandbox sessions"
```

---

### Task 5: Git Result Service And Readonly Guard

**Files:**
- Create: `gepa_researcher/execution/git_result.py`
- Create: `tests/test_git_result_service.py`
- Modify: `gepa_researcher/storage/provenance.py`

**Interfaces:**
- Consumes: `ExecutionSpec`, `SandboxSession`, candidate policy, existing `audit_commit`.
- Produces:
  - `GitResultService.finalize_implementation(spec, session) -> tuple[str | None, CommitAudit]`
  - `GitResultService.assert_readonly_unchanged(spec, session) -> None`

- [ ] **Step 1: Write failing git result tests**

Add tests for:

```python
def test_finalize_records_result_revision_and_frozen_violation(tmp_path):
    repo, baseline = make_repo(tmp_path)
    session = materialize_session(repo, baseline, tmp_path)
    commit_change(session.repo_path, "tests/fixture.root", "tampered")
    service = GitResultService(candidate_policy={"frozen_globs": ["tests/**"], "max_commits": 1})

    result_sha, audit = service.finalize_implementation(make_spec(baseline), session)

    assert result_sha == audit.result_sha
    assert "tests/fixture.root" in audit.frozen_violations
```

Add a readonly test:

```python
def test_readonly_guard_rejects_head_or_tracked_source_change(tmp_path):
    session = materialize_session(...)
    before = service.snapshot(session)
    mutate_tracked_file_without_commit(session.repo_path)
    with pytest.raises(RuntimeError, match="read-only execution changed sandbox"):
        service.assert_readonly_unchanged(make_feedback_spec(), session, before)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_git_result_service.py -q`

Expected: FAIL because service does not exist.

- [ ] **Step 3: Implement service**

Implementation rules:

- For implementation phase, use `audit_commit(repo=session.repo_path, parent_sha=spec.input_revision, frozen_globs=...)`.
- If commit count exceeds `candidate_policy.max_commits`, record a typed failure later in ExecutionService; this service returns audit data.
- For readonly evaluation phases, snapshot `HEAD` and `git status --porcelain=v1 --untracked-files=no` before runner; assert both are unchanged after runner.
- Runtime debris and untracked files are not hard failures for implementation finalization.

- [ ] **Step 4: Run tests**

Run:

```bash
python -m pytest tests/test_git_result_service.py -q
python -m pytest tests/test_p0_safety.py::WorkspaceAndProvenanceTest::test_audit_commit_records_changed_files_and_ignores_runtime_debris -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gepa_researcher/execution/git_result.py tests/test_git_result_service.py gepa_researcher/storage/provenance.py
git commit -m "feat: add git result service"
```

---

### Task 6: ExecutionService And RunnerAdapter

**Files:**
- Create: `gepa_researcher/services/__init__.py`
- Create: `gepa_researcher/services/execution_service.py`
- Modify: `gepa_researcher/agents/adapters.py`
- Modify: `gepa_researcher/agents/agent_components.py`
- Create: `tests/test_execution_service.py`

**Interfaces:**
- Consumes: `ExecutionSpec`, `CandidateCard`, `RepositoryMaterializer`, runtime backend, agent executor.
- Produces:
  - `ExecutionService.execute(spec: ExecutionSpec, card: CandidateCard) -> tuple[ExecutionRecord, Trace]`
  - `RunnerAdapter.run(card: CandidateCard, spec: ExecutionSpec, runtime_lease: RuntimeLease, session: SandboxSession) -> Trace`

- [ ] **Step 1: Write failing ExecutionService tests**

Add `tests/test_execution_service.py` with two tests:

```python
def test_implementation_execution_creates_result_revision_and_updates_store(tmp_path):
    service = make_execution_service(tmp_path, executor=CommittingExecutor())
    card = make_card(base_revision=baseline)
    spec = make_implementation_spec(card)

    record, trace = service.execute(spec, card)

    assert record.status == ExecutionStatus.SUCCEEDED
    assert record.result_revision is not None
    assert record.execution_id in [r.execution_id for r in execution_store.list_for_candidate(card.candidate_id)]
    assert trace.candidate_id == card.candidate_id
```

```python
def test_feedback_execution_uses_result_revision_as_readonly_input(tmp_path):
    card = make_card(base_revision=baseline, result_revision=child_sha)
    spec = make_feedback_spec(card, input_revision=child_sha)
    record, trace = service.execute(spec, card)

    assert record.phase == ExecutionPhase.FEEDBACK_EVAL
    assert record.input_revision == child_sha
    assert record.result_revision is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_execution_service.py -q`

Expected: FAIL because service does not exist.

- [ ] **Step 3: Implement ExecutionService**

Execution flow:

```python
record = execution_store.create_pending(spec)
try:
    execution_store.mark_preparing(spec.execution_id)
    session = materializer.materialize(spec)
    runtime_lease = runtime_backend_for(config, run_dir).prepare(spec, session, record)
    execution_store.mark_running(spec.execution_id)
    before = git_result_service.snapshot(session) if not spec.capability_policy.repo_writable else None
    trace = runner.run(card, spec, runtime_lease, session)
    execution_store.mark_collecting(spec.execution_id)
    if spec.phase == ExecutionPhase.IMPLEMENTATION:
        result_revision, audit = git_result_service.finalize_implementation(spec, session)
    else:
        git_result_service.assert_readonly_unchanged(spec, session, before)
        result_revision = None
    return execution_store.mark_succeeded(...), trace
except Exception as exc:
    return execution_store.mark_failed(spec.execution_id, mapped_failure), failure_trace
finally:
    cleanup_service.cleanup(session)
```

`RunnerAdapter` must be the only place that builds transient config keys for `AgentExecutor`. It may still pass `_candidate_repo`, `_candidate_workspace`, `_executor_command_prefix`, and `_execution_id`, but those keys must not be stored as CandidateCard identity.

- [ ] **Step 4: Run ExecutionService tests**

Run: `python -m pytest tests/test_execution_service.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gepa_researcher/services/execution_service.py gepa_researcher/services/__init__.py gepa_researcher/agents/adapters.py gepa_researcher/agents/agent_components.py tests/test_execution_service.py
git commit -m "feat: route execution through execution service"
```

---

### Task 7: CandidateFactory And CandidateScheduler

**Files:**
- Create: `gepa_researcher/services/candidate_factory.py`
- Create: `gepa_researcher/services/candidate_scheduler.py`
- Create: `tests/test_candidate_scheduler.py`

**Interfaces:**
- Consumes: `CandidateCard`, `ProposalIdea`, parent cards, execution store.
- Produces:
  - `CandidateFactory.create_seed(round_id, proposal, baseline_revision) -> CandidateCard`
  - `CandidateFactory.create_child(round_id, parent_cards, proposal, code_base_parent_id) -> CandidateCard`
  - `CandidateScheduler.make_implementation(card) -> ExecutionSpec`
  - `CandidateScheduler.make_feedback_eval(card, dataset_ref) -> ExecutionSpec`
  - `CandidateScheduler.make_pareto_eval(card, dataset_ref) -> ExecutionSpec`
  - `CandidateScheduler.next_execution(card) -> ExecutionSpec | None`

- [ ] **Step 1: Write failing factory and scheduler tests**

Add tests:

```python
def test_child_base_revision_uses_code_base_parent_result_revision():
    parent_a = make_card("parent-a", result_revision="a" * 40)
    parent_b = make_card("parent-b", result_revision="b" * 40)
    child = factory.create_child(
        round_id=1,
        parent_cards=[parent_a, parent_b],
        proposal=proposal,
        code_base_parent_id="parent-b",
    )

    assert child.parent_candidate_ids == ("parent-a", "parent-b")
    assert child.base_revision == "b" * 40
```

```python
def test_scheduler_uses_base_revision_for_implementation_and_result_revision_for_eval():
    card = make_card(status=CandidateStatus.ADMITTED, base_revision="a" * 40)
    impl = scheduler.make_implementation(card)
    assert impl.phase == ExecutionPhase.IMPLEMENTATION
    assert impl.input_revision == "a" * 40

    card.result_revision = "b" * 40
    card.status = CandidateStatus.MATERIALIZED
    feedback = scheduler.make_feedback_eval(card, dataset_ref="feedback:round-1")
    assert feedback.phase == ExecutionPhase.FEEDBACK_EVAL
    assert feedback.input_revision == "b" * 40
    assert feedback.capability_policy.repo_writable is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_candidate_scheduler.py -q`

Expected: FAIL because factory and scheduler do not exist.

- [ ] **Step 3: Implement factory and scheduler**

Candidate ID policy:

- Seeds keep current `seed_000` style.
- Children keep current `cand_<round>_<index>` style.
- Execution IDs use `exec_<round>_<candidate_id>_<phase>_<short_uuid>`.

Scheduling rules:

- `ADMITTED` -> implementation from `base_revision`.
- `MATERIALIZED` -> feedback eval from `result_revision`.
- Feedback-passed-but-pareto-missing -> pareto eval from `result_revision`.
- Terminal statuses return `None`.

- [ ] **Step 4: Run scheduler tests**

Run: `python -m pytest tests/test_candidate_scheduler.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gepa_researcher/services/candidate_factory.py gepa_researcher/services/candidate_scheduler.py tests/test_candidate_scheduler.py
git commit -m "feat: add candidate factory and scheduler"
```

---

### Task 8: Orchestrator Uses CandidateCard And ExecutionService

**Files:**
- Modify: `gepa_researcher/orchestrator.py`
- Modify: `gepa_researcher/storage/pool.py`
- Modify: `gepa_researcher/storage/store.py`
- Modify: `gepa_researcher/loop/context_views.py`
- Modify: `gepa_researcher/display.py`
- Create: `tests/test_candidate_kernel_orchestrator.py`

**Interfaces:**
- Consumes: services from Tasks 2, 6, and 7.
- Produces: full loop behavior where accepted pool references CandidateCards with `result_revision`.

- [ ] **Step 1: Write failing orchestrator tests**

Add `tests/test_candidate_kernel_orchestrator.py`:

```python
def test_seed_requires_result_revision_before_active_pool(tmp_path):
    orchestrator = make_orchestrator_with_non_committing_executor(tmp_path)

    with pytest.raises(RuntimeError, match="no valid seeds"):
        orchestrator.run()
```

```python
def test_generation_child_inherits_parent_result_revision(tmp_path):
    orchestrator = make_orchestrator_with_committing_executor(tmp_path)
    state = orchestrator.run()

    child_cards = CandidateStore(orchestrator.run_dir).list_by_round(0)
    parent = CandidateStore(orchestrator.run_dir).get(child_cards[0].parent_candidate_ids[0])
    assert child_cards[0].base_revision == parent.result_revision
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_candidate_kernel_orchestrator.py -q`

Expected: FAIL because orchestrator still uses legacy `CandidatePool` and `ExecutionRegistry`.

- [ ] **Step 3: Replace registry/pool execution identity**

Changes:

- Replace `self.registry = ExecutionRegistry(...)` with `CandidateStore`, `ExecutionStore`, and `ArtifactStore`.
- Replace `_candidate_has_stackable_result(candidate_id)` with lookup of `CandidateCard.result_revision`.
- Store `CandidateCard` at proposal/admission time.
- On successful implementation, update card to `MATERIALIZED` and set `result_revision`.
- On feedback/pareto evaluation, append execution IDs but do not change `result_revision`.
- Keep `ScoreMatrixBuilder`, `GEPAGate`, and `ParetoSelector` behavior unchanged.

- [ ] **Step 4: Update context view mappings**

`candidate_for_agent`, `candidate_for_executor`, and `candidate_for_judger` must read proposal facts from `CandidateCard.proposal` and include:

```python
{
    "candidate_id": card.candidate_id,
    "round_id": card.round_id,
    "parent_candidate_ids": list(card.parent_candidate_ids),
    "base_revision": card.base_revision,
    "result_revision": card.result_revision,
    "status": card.status,
    "hypothesis": card.proposal.hypothesis,
    ...
}
```

Do not include sandbox paths.

- [ ] **Step 5: Run orchestrator and mini-flow tests**

Run:

```bash
python -m pytest tests/test_candidate_kernel_orchestrator.py -q
python -m pytest tests/test_gepa_mini_flow.py -q
python -m pytest tests/test_smoke.py -q
```

Expected: PASS after tests are migrated away from old path assertions.

- [ ] **Step 6: Commit**

```bash
git add gepa_researcher/orchestrator.py gepa_researcher/storage/pool.py gepa_researcher/storage/store.py gepa_researcher/loop/context_views.py gepa_researcher/display.py tests/test_candidate_kernel_orchestrator.py tests/test_gepa_mini_flow.py tests/test_smoke.py
git commit -m "refactor: orchestrate candidate cards through execution service"
```

---

### Task 9: Remove Legacy Registry, Workspace Reuse, And Lifecycle Config

**Files:**
- Delete: `gepa_researcher/storage/registry.py`
- Modify: `gepa_researcher/execution/workspace.py`
- Modify: `gepa_researcher/config/resolver.py`
- Modify: `tests/test_config_system.py`
- Modify: `tests/test_p0_safety.py`
- Modify: `tests/test_runtime_backend.py`
- Modify: imports across `gepa_researcher/` and `tests/`

**Interfaces:**
- Consumes: new stores and materializer.
- Produces: no remaining production reference to `ExecutionRegistry`, `WorkspaceLease`, `materialize_once`, or candidate-keyed workspace reuse.

- [ ] **Step 1: Write failing removal assertions**

Add tests or update existing tests to assert:

```python
def test_resolver_does_not_emit_execution_lifecycle(tmp_path):
    config = load_and_resolve(task_path)
    assert "execution" not in config or "lifecycle" not in config.get("execution", {})
```

And:

```python
def test_no_legacy_registry_module_imports():
    import pathlib
    root = pathlib.Path("gepa_researcher")
    text = "\n".join(path.read_text() for path in root.rglob("*.py"))
    assert "ExecutionRegistry" not in text
    assert "materialize_once" not in text
```

- [ ] **Step 2: Run targeted tests to verify failures**

Run:

```bash
python -m pytest tests/test_config_system.py::ConfigSystemTest::test_resolver_applies_defaults_paths_git_sha_and_safety_ceiling -q
python -m pytest tests/test_p0_safety.py -q
```

Expected: FAIL where old registry/lifecycle expectations remain.

- [ ] **Step 3: Remove old modules and old tests**

Remove or rewrite:

- `test_registry_only_resolves_accepted_result_sha`
- `test_materializes_once_then_uses_evaluate_only`
- `test_existing_worktree_must_be_clean_before_evaluate_only`

Keep or rewrite:

- frozen path admission tests
- LFS pre-materialization tests, but against `RepositoryMaterializer`
- controller protection tests, if `ControllerGuard` remains in `workspace.py`
- commit audit tests
- runtime env redaction tests

- [ ] **Step 4: Run repository search checks**

Run:

```bash
rg -n "ExecutionRegistry|WorkspaceLease|materialize_once|record_workspace|accepted_result_sha|workspaces\\[candidate_id\\]|executions\\[candidate_id\\]" gepa_researcher tests
```

Expected: no production references; test references only where asserting absence.

- [ ] **Step 5: Run migrated test set**

Run:

```bash
python -m pytest tests/test_candidate_execution_domain.py tests/test_candidate_execution_stores.py tests/test_execution_materializer.py tests/test_git_result_service.py tests/test_execution_service.py tests/test_candidate_scheduler.py tests/test_candidate_kernel_orchestrator.py -q
python -m pytest tests/test_config_system.py tests/test_runtime_backend.py tests/test_p0_safety.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add gepa_researcher tests
git rm gepa_researcher/storage/registry.py
git commit -m "refactor: remove legacy candidate workspace lifecycle"
```

---

### Task 10: Final Integration, Docs, And Regression Run

**Files:**
- Modify: `docs/GEPA_candidate_card_opt.md`
- Modify: `docs/GEPA_v1_routine.md`
- Modify: `README.md`
- Modify: `examples/omilrec/*.yaml`

**Interfaces:**
- Consumes: completed implementation.
- Produces: documentation that matches new lifecycle and examples without `execution.lifecycle`.

- [ ] **Step 1: Update docs with actual object names**

In `docs/GEPA_candidate_card_opt.md`, add a short “Implemented v1.0.5 shape” section listing:

```text
CandidateCard -> CandidateStore
ExecutionSpec -> ExecutionService
ExecutionRecord -> ExecutionStore
SandboxSession -> RepositoryMaterializer
ArtifactRef -> ArtifactStore
GitResultService -> commit audit and readonly guard
```

- [ ] **Step 2: Update examples**

Remove any `execution.lifecycle`, `workspace.root`, or `branch_prefix` fields from examples unless they are still accepted as non-authoritative materializer tuning. Keep source path/ref, isolation, safety, metric, validation, and loop settings.

- [ ] **Step 3: Run full test suite**

Run:

```bash
python -m pytest tests -q
```

Expected: PASS.

- [ ] **Step 4: Run import smoke**

Run:

```bash
python -m gepa_researcher.cli --help
```

Expected: help text prints and exits 0.

- [ ] **Step 5: Search for forbidden legacy vocabulary**

Run:

```bash
rg -n "materialize_once|ExecutionRegistry|record_workspace|accepted_result_sha|workspace_lease|candidate worktree|canonical execution" gepa_researcher tests docs README.md examples
```

Expected: no production references; docs may mention legacy terms only in migration notes that explicitly mark them removed.

- [ ] **Step 6: Commit**

```bash
git add docs README.md examples
git commit -m "docs: document candidate execution kernel"
```

---

## Self-Review

**Spec coverage:** This plan covers CandidateCard, RevisionRef, ExecutionSpec, ExecutionRecord, ArtifactRef, CandidateStore, ExecutionStore, ArtifactStore, CandidateFactory, CandidateScheduler, ExecutionService, per-execution SandboxSession, removal of candidate-owned workspace, removal of `materialize_once`, parent lineage versus code base revision, runtime backend adaptation, orchestrator migration, config cleanup, tests, and docs.

**Placeholder scan:** No task contains TBD/TODO/fill-later instructions. Each task names exact files, exact commands, expected failures, expected passes, and concrete interface names.

**Type consistency:** `CandidateCard`, `ExecutionSpec`, `ExecutionRecord`, `SandboxSession`, `RepositoryMaterializer`, `GitResultService`, `CandidateFactory`, `CandidateScheduler`, and `ExecutionService` names are consistent across tasks. `result_revision` is the authoritative candidate output SHA; `input_revision` is the authoritative execution input SHA.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-14-candidate-execution-kernel.md`. Two execution options:

1. **Subagent-Driven (recommended)** - dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** - execute tasks in this session using executing-plans, batch execution with checkpoints.

