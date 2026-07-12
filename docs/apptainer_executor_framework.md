# Apptainer Executor Framework

## 1. Background and Root Cause

Apptainer support must be treated as a runtime framework for the GEPA loop, not
as a `command_prefix` appended before the executor command. The executor is a
Claude Code agent with its own HOME, auth state, shell environment, writable
runtime files, project mounts, artifact paths, and admission/audit contracts.
Those concerns cross several loop stages.

The first integration failures exposed that boundary clearly:

- The task profile did not enable `execution.runtime_backend: apptainer`, so the
  loop silently stayed on the local backend.
- Adding only an Apptainer command prefix started a container, but Claude could
  not see valid auth or HOME state.
- Binding host `.claude` read-only fixed auth discovery, but Claude then failed
  when it needed to write runtime files such as `.claude/session-env`.
- A later run failed before executor execution because the Admission Gate
  rejected all seed proposals. That was not a container startup bug; it showed
  that proposer schema, resolved policy defaults, gate checks, and executor
  runtime must stay consistent when the backend changes.

The root issue is architectural: Apptainer changes the execution contract for
the full loop. It must own discovery, materialization, agent initialization,
project mounting, preflight checks, runtime leases, and diagnostics.

## 2. Goals and Non-goals

V1 goals:

- When a config requests Apptainer, the loop must either run with Apptainer or
  fail early with actionable diagnostics. It must not silently fall back to local
  execution.
- Separate the Claude agent runtime from the user's project runtime.
- Create a writable, per-execution HOME for Claude and initialize only the
  minimal auth/config state required for the agent to run.
- Prepare a per-candidate `RuntimeLease` containing repo, artifacts, scratch,
  HOME, binds, env, and command construction.
- Validate Apptainer, Claude, binds, write paths, project tools, and admission
  policy before expensive loop work begins.
- Persist enough diagnostics to explain runtime failures after the run.

Non-goals for V1:

- Build a general container orchestration system.
- Support arbitrary mutable host HOME binds.
- Make Apptainer responsible for proposer or gate logic.
- Hide project dependency problems by installing tools opportunistically inside
  candidate containers.

## 3. Framework Layers

### Host Runtime Layer

This layer prepares the host-side execution substrate:

- Discover `execution.apptainer.executable`, `GEPA_APPTAINER`, `apptainer`, or
  `singularity`.
- Optionally run a configured site install hook, then probe again.
- Detect whether normal exec works or `--userns` is required.
- Build or reuse the executor SIF from the resolved config.
- Resolve the host Claude binary and the node/runtime tree it needs.
- Diagnose host Claude auth availability before loop execution.

This layer belongs primarily in `container_image.py` and the CLI doctor/setup
entry points.

### Agent Runtime Layer

This layer prepares the Claude Code agent environment inside the container:

- Allocate a per-execution writable HOME.
- Initialize Claude state from a minimal host/template source.
- Copy auth/config files that are safe to reuse.
- Exclude volatile host state such as sessions, shell snapshots, caches,
  histories, and debug files.
- Ensure `.claude/session-env` and other runtime paths are writable.
- Run a minimal Claude probe, including a Bash tool write to artifacts, before
  treating the runtime as healthy.

This layer is distinct from the project environment. Its job is to make the
executor agent work, not to install or mutate the optimized project.

### Project Runtime Layer

This layer exposes the candidate project to the executor:

- Bind the candidate worktree to the configured container repo path.
- Bind artifacts to the configured container artifacts path.
- Provide per-candidate scratch and temp directories.
- Bind read-only assets such as `/cvmfs` only when required by resolved
  commands or explicit config.
- Validate project-facing commands and paths: shell, Python, build tools,
  metric commands, validation checks, and setup commands.
- Confirm the executor can write artifacts and scratch files from inside the
  container.

Project runtime validation must not depend on Claude auth. Conversely, Claude
agent validation must not imply that project tools are present.

### Loop Integration Layer

This layer connects runtime state to the GEPA loop:

- The resolved config must expose backend choice and all runtime defaults.
- Proposer, Admission Gate, executor, audit, and reports must share the same
  resolved candidate policy defaults.
- Executor preparation must receive a `RuntimeLease` rather than a raw string
  prefix.
- Runtime diagnostics must be included in config snapshots or run artifacts.
- Admission sanity should run before executor scheduling so seed candidates are
  not all rejected by hidden default mismatches.

This layer belongs across config resolution, orchestrator startup, executor
adapter preparation, and admission/audit logging.

## 4. Runtime Lifecycle

The intended lifecycle is:

1. **Resolve config**: normalize `runtime_backend`, Apptainer image settings,
   binds, env allowlist, Claude HOME policy, and candidate policy defaults.
2. **Discover host runtime**: locate Apptainer, determine `--userns` needs, and
   resolve host Claude.
3. **Materialize image**: build or reuse the SIF, using a cache fingerprint that
   reflects the base image, required tools, and Claude runtime dependency.
4. **Initialize agent HOME**: create a per-execution writable HOME and populate
   minimal Claude auth/config state.
5. **Preflight**: run host, agent, project, and loop-policy checks.
6. **Prepare lease**: create a `RuntimeLease` for each candidate with concrete
   host paths, container paths, env, binds, and command construction.
7. **Execute**: run executor commands through the lease.
8. **Audit**: record runtime backend, command prefix, binds, environment
   summary, diagnostics, artifacts, and frozen-source checks.

Any failure before step 6 is a loop startup/preflight failure. Failures at or
after step 6 are candidate execution failures unless they indicate global runtime
corruption.

## 5. Configuration Contract

The minimum Apptainer contract is:

```yaml
execution:
  runtime_backend: apptainer
  apptainer:
    auto_image: true
    image: null
    command: claude
    cleanenv: true
    containall: true
    writable_tmpfs: true
    container_repo: /workspace/repo
    container_artifacts: /workspace/artifacts
    auto_init_claude_home: true
```

Rules:

- `runtime_backend: apptainer` is authoritative. If Apptainer cannot be prepared,
  validation/doctor/run must fail loudly.
- `auto_image: false` requires an explicit image path.
- Claude HOME initialization defaults to enabled for Apptainer because the loop
  cannot run without a writable agent HOME.
- Read-only binds are allowed for project assets, not for Claude runtime state
  that the agent may mutate.
- Environment variables must be passed through an allowlist. `HOME` is controlled
  by Apptainer `--home`, not by `--env HOME=...`.
- Candidate policy defaults must be present in the resolved config and shared by
  proposer and Admission Gate. For the current optimizer, `safe-source` and
  `algorithmic` candidates must be treated consistently with the proposer schema.

## 6. Preflight Matrix

| Area | Check | Failure handling |
|---|---|---|
| Apptainer binary | executable exists and can run `exec` | fail before loop |
| Host capability | normal exec or `--userns` works | fail before loop |
| Image | SIF exists or can be materialized | fail before loop |
| Claude binary | host command resolves and can be bound | fail before loop |
| Claude auth/config | minimal auth/config is available or template is usable | fail before loop |
| Claude writable HOME | `.claude/session-env` can be created in per-exec HOME | fail before loop |
| Claude tool path | `claude -p` can invoke Bash in the container | fail before loop |
| Artifact write | Bash inside Apptainer writes to container artifacts path | fail before loop |
| Project binds | repo, artifacts, scratch, temp, and read-only binds resolve | fail before loop |
| Project tools | configured setup/metric/validation commands have required tools | fail before loop when globally missing; candidate failure when candidate-specific |
| Admission sanity | seed candidate classes match resolved policy | fail before executor scheduling |
| Audit | runtime metadata can be persisted | fail before loop if run directory is not writable |

The doctor command should run as much of this matrix as possible without starting
a full optimization. The run path should repeat required checks instead of
assuming doctor was run manually.

## 7. Failure Policy

Global runtime failures stop the loop before candidates are consumed:

- missing Apptainer executable
- unusable setuid/userns capability
- missing or invalid image
- Claude cannot start inside the container
- agent HOME is not writable
- artifacts/scratch are not writable
- resolved policy would reject all initial seed candidates

Candidate-scoped failures are recorded against the candidate:

- candidate code breaks build or validation
- candidate edits violate frozen-source policy
- candidate-specific commands time out or return nonzero
- candidate-generated files are missing or malformed

Every runtime failure should include:

- backend name and Apptainer executable
- image path and materialization mode
- `--userns`/containment flags
- repo/artifacts/scratch/HOME host and container paths
- selected binds
- sanitized env summary
- failed probe command and stderr excerpt

Diagnostics should be written to the run directory alongside the config snapshot
and per-candidate executor metadata.

## 8. Current Implementation Mapping

`gepa_researcher/execution/container_image.py` should own Host Runtime Layer
behavior:

- Apptainer discovery
- install hook probing
- image materialization
- host capability probes
- doctor/setup diagnostics

`gepa_researcher/execution/runtime_backend.py` should own lease construction and
Agent/Project Runtime preparation:

- per-execution HOME, scratch, and artifacts paths
- Claude HOME initialization
- Apptainer command construction
- bind/env normalization
- runtime metadata returned through `RuntimeLease`

`gepa_researcher/cli.py` should expose explicit operator entry points:

- `validate` resolves config and catches invalid backend contracts
- `doctor` reports runtime readiness and actionable failures
- `setup-apptainer` materializes and probes the image
- `run` repeats required startup checks before launching the loop

The executor adapter should consume a `RuntimeLease`:

- prepare the lease before executor invocation
- store executor/runtime metadata in candidate records
- treat lease preparation failure as runtime failure when global, or candidate
  failure when tied to candidate-specific state

The config resolver should be the single source for policy defaults:

- proposer schema and Admission Gate must see the same candidate classes
- resolved config snapshots must include defaults explicitly
- changing runtime backend must not silently change admission behavior

## 9. Implementation Roadmap

1. **Document and freeze the contract**: keep this framework document as the
   baseline for Apptainer executor changes.
2. **Make preflight first-class**: expose a shared preflight routine used by
   doctor, setup, validate, and run.
3. **Promote Claude HOME init to a framework step**: keep writable per-execution
   HOME as the default Apptainer behavior, with no read-only `.claude` runtime
   bind.
4. **Split agent and project probes**: independently test Claude/Bash/artifact
   behavior and project command/tool availability.
5. **Harden resolved policy defaults**: ensure candidate classes and safety
   classes are explicit in snapshots and identical across proposer/gate.
6. **Persist runtime diagnostics**: record preflight and lease metadata in a
   stable artifact file for every run.
7. **Add smoke coverage**: keep unit tests for lease construction and add an
   optional real-Apptainer probe for environments that provide Apptainer.

## 10. Acceptance Criteria

Documentation acceptance:

- `docs/apptainer_executor_framework.md` exists.
- It describes Apptainer as a GEPA runtime framework, not a command prefix.
- It separates Claude agent runtime from user project runtime.
- It covers Host, Agent, Project, and Loop Integration layers.
- It names the current failure chain and the architectural root cause.
- It includes preflight, failure policy, implementation mapping, and acceptance
  criteria.

Implementation acceptance for future code changes:

```bash
python -m gepa_researcher.cli validate --config <config> --no-materialize
python -m gepa_researcher.cli doctor --config <config>
python -m unittest tests.test_runtime_backend tests.test_config_system tests.test_p0_safety -v
```

Real-runtime acceptance when Apptainer is available:

- Apptainer can execute the configured SIF.
- Claude starts inside the container with initialized writable HOME.
- Claude can use Bash to write into `$GEPA_ARTIFACTS`.
- Seed proposals pass Admission Gate under the resolved policy.
- A BR111 full-loop smoke run reaches executor execution and records runtime
  diagnostics in the run artifacts.
