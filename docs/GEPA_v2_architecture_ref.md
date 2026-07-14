# GEPA v2.0 Architecture Reference

## 0. Purpose

This document is the implementation-facing reference for upgrading GEPA from
v1.2 Global Context Plane to v2.0 Stateful Agentic GEPA.

It does not replace `docs/GEPA_v1_routine.md`. The routine document describes
the full v1.x to v2.0 roadmap; this document narrows that roadmap into concrete
architecture guidance for the next implementation phase.

The central v2.0 goal is:

> Proposal direction, planning quality, execution quality, critique quality, and
> final value should be separately observable and attributable.

## 1. Naming Convention

The roadmap uses the conceptual name `Optimizer`, but the project should keep
the existing top-level name `Proposer`.

Use this mapping everywhere in code, config, events, logs, and prompts:

| Roadmap concept | Project name |
|---|---|
| Optimizer | Proposer |
| Judge | Judger |
| Executor | Executor |

Do not introduce a new top-level `Optimizer` module or role name in v2.0. The
existing `proposer` role expands toward optimizer-like responsibility, but its
project identity remains `proposer`.

The v2.0 role model is therefore:

```text
Proposer
    ↓ ProposalIdea

Executor
├── Planner
├── Runner
└── Critic
    ↓ ExecutionSubmission

Judger
    ↓ Relative Judgment
```

Top-level orchestration remains:

```text
Proposer / Executor / Judger
```

The new roles `Planner`, `Runner`, and `Critic` are executor-internal roles, not
new top-level subsystems.

## 2. What v2.0 Reuses From v1.2

v2.0 should build on the v1.0.5, v1.1, and v1.2 kernels instead of replacing
them.

Reuse as-is or with narrow extensions:

- `CandidateCard`
- `ProposalIdea`
- `RevisionRef`
- `ExecutionSpec`
- `ExecutionRecord`
- `ExecutionStore`
- `EventStore`
- `ArtifactStore`
- `ArtifactRef`
- `TypedExecutionFailure`
- `WorkspaceManager`
- `RepositoryMaterializer`
- `GitResultService`
- `GlobalContextPlane`
- `ContextBlock`
- `ContextView`
- `ContextViewBuilder`
- `PromptAssembler`
- `PresentationStream`
- `AgentProposer`
- `AgentExecutor`
- `AgentJudger`
- `RunnerAdapter`
- `JudgerAdapter`

Current role reuse:

- Existing `AgentProposer` becomes the v2.0 Proposer baseline.
- Existing `AgentExecutor` becomes the first Runner implementation.
- Existing `AgentJudger` remains the Judger baseline, then evolves toward
  parent-relative scoring.

The important architectural change is not a new storage layer. It is the
executor-internal loop and the stricter separation between direction, plan,
implementation, critique, and value judgment.

## 3. Target v2.0 Flow

Current v1.2 flow:

```text
Proposer
    ↓ Candidate
Executor
    ↓ Trace
Judger
    ↓ Judgment
Gate / Pareto
```

v2.0 target flow:

```text
Proposer
    ↓ ProposalIdea

Executor receives ProposalIdea
    ↓
Planner builds ExecutionPlan
    ↓
Runner executes ExecutionAttempt
    ↓
Critic reviews attempt
    ├── continue -> Runner executes another attempt
    ├── replan   -> Planner builds revised plan
    ├── submit   -> ExecutionSubmission
    └── abort    -> typed failure

Judger evaluates ExecutionSubmission against parent/baseline
    ↓
Gate / Pareto / ScoreMatrix
    ↓
Proposer receives structured feedback for later proposals
```

This should keep the outer loop recognizable while making the inside of
Executor stateful and inspectable.

## 4. New Domain Objects

### 4.1 ExecutionPlan

`ExecutionPlan` is the contract between Planner and Runner.

Minimum fields:

```yaml
execution_plan:
  plan_id:
  candidate_id:
  proposal_id:
  round_id:
  objective:
  target_scope:
  target_files:
  inspect_steps:
  implementation_steps:
  validation_steps:
  allowed_deviations:
  forbidden_changes:
  expected_artifacts:
  completion_criteria:
  risk_notes:
  budget:
  source_refs:
```

Hard requirements:

- It must not contain raw planner prose outside typed fields.
- It must include `candidate_id`, `proposal_id`, and `round_id`.
- It must name target files or target globs.
- It must state forbidden changes.
- It must state completion criteria.
- It must be serializable and event-recorded.

Planner may inspect context and source summaries, but Planner must not modify
files or run project-changing commands.

### 4.2 ExecutionAttempt

`ExecutionAttempt` records one Runner attempt under one plan.

Minimum fields:

```yaml
execution_attempt:
  attempt_id:
  plan_id:
  candidate_id:
  execution_id:
  started_at:
  finished_at:
  actions:
  commands_run:
  changed_files:
  metrics:
  validation:
  errors:
  artifact_refs:
  commit_sha:
```

Hard requirements:

- Attempts are append-only.
- Retry means a new attempt id, not overwriting the previous attempt.
- Attempt artifacts should be indexed through `ArtifactStore`.
- Attempt lifecycle events should be appended to `EventStore`.

### 4.3 CritiqueDecision

`CritiqueDecision` is the contract between Critic and the executor loop.

Allowed verdicts:

```text
continue
replan
submit
abort
```

Minimum fields:

```yaml
critique_decision:
  decision_id:
  attempt_id:
  plan_id:
  candidate_id:
  verdict:
  plan_adherence:
  implementation_gaps:
  validation_gaps:
  unresolved_errors:
  scope_violations:
  feedback_to_planner:
  feedback_to_runner:
  confidence:
```

Hard requirements:

- Critic cannot edit source files.
- Critic cannot run corrective implementation commands.
- Critic cannot assign final optimization value.
- Critic decides whether the executor loop should continue, replan, submit, or
  abort.

### 4.4 ExecutionSubmission

`ExecutionSubmission` is what the executor sends to Judger.

Minimum fields:

```yaml
execution_submission:
  submission_id:
  candidate_id:
  final_plan_id:
  final_attempt_id:
  result_revision:
  changed_files:
  metrics:
  validation:
  artifact_refs:
  known_limitations:
  critic_summary:
```

Hard requirements:

- It must refer to the final attempt and result revision.
- It must not rely on uncommitted workspace state.
- It should summarize evidence; large evidence remains in artifacts.

### 4.5 Relative Judgment

The existing `Judgment` can be extended before introducing a new class. The
important shift is semantic: Judger evaluates value relative to parent or
baseline.

Recommended fields:

```yaml
relative_judgment:
  candidate_id:
  parent_candidate_ids:
  baseline_revision:
  parent_score:
  candidate_score:
  delta:
  uncertainty:
  hard_gates_passed:
  evidence_complete:
  dimensions:
    task_performance:
    robustness:
    correctness:
    generality:
    cost:
    maintainability:
  direction_feedback:
  implementation_feedback:
  confidence:
```

The Judger should answer three questions separately:

1. Is the submission valid?
2. How much better or worse is it than parent/baseline?
3. Why did it improve, fail, or remain inconclusive?

## 5. Context View Extensions

v1.2 established `GlobalContextPlane`, `ContextViewBuilder`, and
`PromptAssembler`. v2.0 should extend this system, not bypass it.

Required views:

| View | Consumer | Purpose |
|---|---|---|
| Proposer view | Proposer | Choose optimization direction and parent mutation |
| Planner view | Planner | Convert proposal into executable plan |
| Runner view | Runner | Execute a plan in a sandbox |
| Critic view | Critic | Decide continue/replan/submit/abort |
| Judger view | Judger | Evaluate final value relative to parent/baseline |
| User view | PresentationStream | Show progress without raw internal noise |

### 5.1 Proposer View

Contains:

- task goal
- current frontier
- parent summaries
- score deltas
- judge feedback
- repeated failure patterns
- prior direction outcomes
- budget summary

Does not contain:

- full raw runner stdout
- hidden judge internals
- code-level implementation plans by default

### 5.2 Planner View

Contains:

- current `ProposalIdea`
- candidate scope and allowed target files
- project resources and relevant docs
- parent implementation summary
- prior attempt summaries for this candidate, if any
- known forbidden paths and budget

Does not contain:

- hidden judge results
- unrelated candidate traces
- authority to change proposal meaning

### 5.3 Runner View

Contains:

- `ExecutionPlan`
- candidate workspace paths
- runtime lease
- selected sample ids
- validation commands and task resources
- allowed files and forbidden files
- prior runner feedback for same candidate/plan

Does not contain:

- other parallel candidates' live results
- hidden judge scoring details
- permission to change plan scope silently

### 5.4 Critic View

Contains:

- `ProposalIdea`
- `ExecutionPlan`
- latest `ExecutionAttempt`
- changed files
- metrics
- validation results
- artifact refs
- error summaries

Does not contain:

- authority to edit files
- final value scoring responsibility
- unrelated global strategy details unless needed for scope checking

### 5.5 Judger View

Contains:

- task goal and rubric
- parent/baseline evidence
- judge-safe candidate facts
- final `ExecutionSubmission`
- metric deltas
- validation evidence
- artifact refs

Does not contain:

- Proposer expected score anchors
- Executor self-congratulating prose as scoring authority
- Critic chain-of-thought or raw private reasoning

## 6. Executor Internal Loop

The executor becomes a small state machine.

```text
PROPOSAL_RECEIVED
    ↓
PLANNING
    ↓
PLAN_READY
    ↓
ATTEMPT_RUNNING
    ↓
ATTEMPT_COMPLETED
    ↓
CRITIQUING
    ├── continue -> ATTEMPT_RUNNING
    ├── replan   -> PLANNING
    ├── submit   -> SUBMISSION_READY
    └── abort    -> ABORTED
```

Terminal failure states:

```text
BUDGET_EXHAUSTED
ENVIRONMENT_BLOCKED
PROPOSAL_INFEASIBLE
SCOPE_VIOLATION
NO_PROGRESS
AGENT_PROTOCOL_INVALID
```

Recommended initial budget controls:

```yaml
executor_loop:
  max_plans: 2
  max_attempts_per_plan: 2
  max_total_attempts: 3
  max_wall_seconds:
  max_files_changed:
```

Keep these defaults conservative. The first v2.0 goal is not more attempts; it
is better attribution of why a candidate failed.

## 7. Capability Isolation

Prompt instructions are not enough. The harness must enforce capabilities.

| Role | May read | May write / execute |
|---|---|---|
| Proposer | global summaries, frontier, judgments, insights | `ProposalIdea` only |
| Planner | proposal, project map, docs, prior attempts | `ExecutionPlan` only |
| Runner | plan, workspace, files, tools | workspace edits and commands |
| Critic | proposal, plan, attempt trace, diff, tests | `CritiqueDecision` only |
| Judger | submission, parent/baseline, rubric, evidence | `Judgment` only |

Hard constraints:

- Proposer does not touch source.
- Planner does not edit source.
- Runner does not read hidden judge results.
- Critic does not modify code.
- Judger does not debug execution.
- Planner is not a top-level subsystem.
- Five roles do not maintain five independent memories.

## 8. Implementation Sequence

Do not implement v2.0 in one large step. Use staged rollout.

### 8.1 v2.0-alpha: Plan Contract + Planner + Old Runner

Goal:

> Separate proposal direction from executable plan.

Changes:

- Add `ExecutionPlan` dataclass/schema.
- Add planner payload validation.
- Add planner context view.
- Add planner prompt assembly.
- Add `AgentPlanner` or planner method under executor components.
- Feed plan into current `AgentExecutor`, which acts as Runner.
- Record plan events and artifacts.

Keep:

- Current single-attempt execution behavior.
- Current Judger scoring.
- Current Gate/Pareto flow.

Acceptance:

- Planner can convert a candidate into a bounded executable plan.
- Runner receives and references the plan.
- Invalid plans become typed failures, not loop crashes.

### 8.2 v2.0-beta: Critic Single Feedback

Goal:

> Decide whether one Runner attempt is submit-worthy before Judger sees it.

Changes:

- Add `CritiqueDecision` dataclass/schema.
- Add critic context view.
- Add critic prompt assembly.
- Add `AgentCritic`.
- Insert critic after Runner.
- First version supports only `submit` or `abort`; `continue` and `replan` can
  be parsed but disabled by budget.

Acceptance:

- Critic catches clear incomplete execution.
- Critic output is recorded as event.
- Critic cannot edit code or replace Judger.

### 8.3 v2.0-gamma: Multi-Attempt Executor Loop

Goal:

> Allow bounded repair/retry/replan inside Executor while preserving auditability.

Changes:

- Add attempt ids.
- Add attempt-level events.
- Enable `continue` and `replan`.
- Add budget controls.
- Add no-progress and repeated-error stopping.
- Build `ExecutionSubmission` from final accepted attempt.

Acceptance:

- Retry creates new attempt records.
- Replan creates new plan records.
- Budget exhaustion is typed and recoverable.
- The final submission points to one final attempt.

### 8.4 v2.0-delta: Relative Judger

Goal:

> Replace loose absolute scoring with parent-relative value judgment.

Changes:

- Extend existing `Judgment` or add `RelativeJudgment`.
- Add parent/baseline evidence to Judger view.
- Add hard metric deltas where available.
- Require delta and uncertainty.
- Track score saturation and same-score rates.

Acceptance:

- Judger distinguishes tiny improvement, meaningful improvement, regression, and
  invalid evidence.
- Judger feedback separates direction feedback from implementation feedback.
- Gate consumes structured score/delta fields, not prose.

### 8.5 v2.0-final: Stateful Proposer Update

Goal:

> Let Proposer learn from direction outcomes without renaming it or giving it
> code-level planning responsibility.

Changes:

- Add proposer strategy state or summary artifact.
- Track direction tags and outcome deltas.
- Feed repeated failure patterns into Proposer view.
- Add diversity and de-duplication policy.

Acceptance:

- Proposer avoids repeatedly failed directions.
- Proposer can explain which parent feedback it is responding to.
- Proposer still outputs abstract `ProposalIdea`, not code-level plans.

## 9. Orchestrator Impact

v2.0 should reduce, not increase, `orchestrator.py` responsibility.

Target responsibility:

- select parents
- request proposals
- schedule candidate executions
- call execution service
- call judger
- update gate/frontier/score matrix
- persist high-level run state

Move out of orchestrator:

- planner prompt details
- runner prompt details
- critic prompt details
- execution attempt loop internals
- role-specific context assembly
- agent protocol parsing
- attempt-level failure mapping

Suggested shape:

```text
ResearchOrchestrator
    -> CandidateScheduler
    -> ExecutionService
        -> ExecutorLoop
            -> PlannerAdapter
            -> RunnerAdapter
            -> CriticAdapter
    -> JudgerAdapter
    -> Gate / Pareto / ScoreMatrix
```

`ExecutionService` should remain the boundary that maps candidate execution into
records, artifacts, and typed failures. `ExecutorLoop` should be responsible for
planner/runner/critic sequencing.

## 10. Testing Strategy

### 10.1 Contract Tests

- valid `ExecutionPlan`
- missing plan fields
- invalid target files
- invalid critic verdict
- invalid submission without final attempt
- relative judgment missing delta

### 10.2 Role Isolation Tests

- Planner cannot produce file edits as accepted output.
- Critic cannot produce source patches as accepted output.
- Runner does not receive hidden judge results.
- Judger does not receive Proposer expected score anchors.
- Proposer does not receive raw runner stdout by default.

### 10.3 Executor Loop Tests

- plan succeeds on first attempt
- runner fails, critic aborts
- runner incomplete, critic continues
- critic requests replan
- budget exhausted
- repeated same error stops loop
- final submission points to final attempt

### 10.4 Judger Calibration Tests

- same metric as parent produces near-zero delta
- clear regression is rejected
- tiny improvement does not saturate score
- meaningful improvement scores above tiny improvement
- missing fresh metric caps score

### 10.5 End-to-End Regression

Keep the current synthetic tests and add a v2.0 variant:

- `tests/test_omilrec_synthetic_loop.py` remains a baseline.
- Add a planner/critic synthetic loop once v2.0-alpha/beta land.
- Verify implementation, feedback, and Pareto phases still produce separate
  executions.
- Verify generated tracked evaluation outputs remain allowed only when declared.

## 11. Risks And Guardrails

### 11.1 Planner Over-Specifies Implementation

Risk:

- Planner starts writing code-level patches or commands that silently narrow the
  search space.

Guardrail:

- Planner writes a plan and constraints, not code.
- Runner owns code edits.
- Critic checks whether implementation stayed within plan.

### 11.2 Critic Becomes A Second Runner

Risk:

- Critic starts fixing code instead of deciding loop control.

Guardrail:

- Critic output schema has no patch field.
- Critic process has no write capability.
- Harness accepts only `CritiqueDecision`.

### 11.3 Judger Remains Score-Saturated

Risk:

- Many candidates score near maximum, giving Proposer weak optimization signal.

Guardrail:

- Require parent-relative delta and uncertainty.
- Use hard metrics where available.
- Track score saturation rate.
- Add anchor examples per task later if needed.

### 11.4 Executor Loop Hides Failure

Risk:

- Multiple attempts make the final result look clean while failed attempts lose
  attribution.

Guardrail:

- Attempts are append-only.
- Critic decisions are events.
- Final submission references final attempt but preserves prior attempt refs.

### 11.5 Premature Stateful Proposer

Risk:

- Proposer becomes more complex before Executor can reliably implement abstract
  ideas, making failures harder to attribute.

Guardrail:

- Implement Planner/Runner/Critic before stateful Proposer upgrades.
- Keep Proposer output abstract through all v2.0 phases.

## 12. Non-Goals

Do not include these in the first v2.0 implementation plan:

- Rename Proposer to Optimizer.
- Make Planner a top-level orchestrator role.
- Add vector database as required infrastructure.
- Add long-term autonomous memory evolution.
- Add a compact/summarizer agent as a required role.
- Let Critic edit code.
- Let Judger debug execution.
- Replace Candidate/Execution/Event/Artifact stores.
- Rewrite config schema.

## 13. Acceptance Checklist

v2.0 is not complete until:

- Proposer outputs abstract proposal direction only.
- Planner converts proposal into executable plan.
- Runner executes plan and records attempts.
- Critic emits continue/replan/submit/abort decisions.
- Executor loop respects budget and records all attempts.
- Judger reports parent-relative value and uncertainty.
- Direction failure, planning failure, runner failure, critique failure, and
  value failure are distinguishable in records.
- Role permissions are harness-enforced.
- Context for all new roles comes through `ContextViewBuilder` and
  `PromptAssembler`.
- Gate/Pareto/ScoreMatrix continue to work with existing v1.2 runs.
- Synthetic end-to-end loop passes.

