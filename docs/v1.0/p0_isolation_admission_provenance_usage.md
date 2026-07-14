# P0 execution safety and agent usage accounting

This version adds four orchestrator-owned boundaries before a code candidate
can influence the GEPA frontier:

    proposal -> admission -> isolated workspace -> execution -> provenance -> judgment

## Candidate admission

candidate_policy is deterministic. Rejected proposals are archived in
admission_decisions.jsonl, but no worktree or executor is created. Common
failure codes include FROZEN_PATH, TARGET_NOT_ALLOWED, PARENT_NOT_ACCEPTED,
and REFUTED_DUPLICATE.

## Git worktrees and provenance

workspace.mode=git_worktree makes the orchestrator resolve the verified parent
commit, create a unique branch/worktree, and pass that worktree as the executor
cwd. Build, install, TEMP, logs, metrics, and artifacts are candidate-local.
The executor must not switch branches or create worktrees.

After execution, the orchestrator independently verifies the start commit,
ancestry, commit budget, admitted diff, dirty files, branch identity, expected
artifacts, and configured binary hashes. A provenance failure receives a
deterministic failing judgment and never reaches the LLM judger.

With execution.lifecycle=materialize_once, the first evaluation uses
implement_and_validate; later feedback/Pareto evaluations use the same verified
commit in evaluate_only mode.

## Token and cost records

Claude is invoked with --output-format json. Each call writes one thread-safe
record to usage/agent_calls.jsonl and optionally preserves its raw envelope.
The ledger separates input, output, cache creation, and cache read tokens, plus
reported USD cost.

processed_tokens is their arithmetic sum and is an observability measure, not a
billable-token claim. Calls without a final usage envelope are recorded as
unavailable, never as zero. Initialization, every round, and the complete run
receive persisted summaries.

## Safe rollout

Do not retrofit an in-flight run. Develop and test in a separate GEPA worktree,
leave its source/config/run directory unchanged, and start a fresh run after
the old orchestrator and descendants exit. Use fake agents and temporary Git
repositories for pre-cutover testing.
