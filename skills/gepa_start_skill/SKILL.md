---
name: gepa_start_skill
description: >-
  Onboard a new task into the GEPA (GEPA-AI-researcher) autonomous
  proposer→executor→judger optimization loop. Use when a user wants to run GEPA
  on a new target project — i.e. asks GEPA to "optimize X", "run GEPA on my
  repo", "start a gepa task", "prepare a gepa package", "how do I set up gepa
  for <project>". Covers the two things a new task needs: (1) assembling a clean,
  self-contained executor resource pack — source, environment, scripts, an
  executor skill, docs, fixtures, pinned baseline — and dry-running it so the
  spawned executor agent can complete the task fully unattended; and (2)
  installing the `gepa` CLI and authoring the task + project-profile config files
  against GEPA's strict schema. GEPA is a general-purpose framework; this skill
  teaches the generic pack-and-launch procedure, never a specific target.
---

# GEPA Start Skill — prepare a task and run the loop

Print `[skill: gepa_start_skill]` before proceeding.

GEPA is an autonomous agent loop: a **proposer** invents candidate changes, an
**executor** agent implements+validates each one against the real project, and a
**judger** scores them. The executor is a spawned `claude -p` subprocess that
must complete the task *with no human in the loop*. That means the user's
project must be packaged as a **clean, self-contained executor resource pack**
that the executor agent can drive by itself — or it will fail mid-run, at the
worst possible moment, for reasons that look like the executor is incompetent
rather than the pack is missing a fixture.

This skill has two parts. Do them in order.

- **Part 1 — Assemble the executor resource pack and dry-run it.** This is the
  hard part and the one that fails in production. A clean pack is the difference
  between a loop that improves the objective and a loop where every candidate
  dies on `resource_missing` or an environment that doesn't resolve.
- **Part 2 — Install GEPA and author the config, then launch.** The config is
  strict: misspelled fields fail at `gepa validate`, not silently.

GEPA is general-purpose. There is a reference pack at
`omilrec_opt/omilrec-br111-executor-pack/` (an OMILREC speed-optimization task)
and OMILREC templates in `examples/omilrec/`. Treat them as *one worked example
of the structure*, not as the structure itself. Every concrete path, command,
tolerance, and pin below is a **placeholder you must replace for the actual
target project.** Never copy OMILREC-specific values (CVMFS paths, `1e-13`,
`4mm/7keV`, `test_fcn`, `time_pdf.bin`) into a different task — those are the
target's contract, not GEPA's.

---

## Part 1 — Assemble the executor resource pack

Goal of Part 1: produce a directory the spawned executor agent can enter and,
unattended, (a) edit the allowed source, (b) build, (c) run every validation
gate, (d) measure the metric, and (e) emit the JSON verdict GEPA expects — all
from package-local resources plus a small set of documented external inputs.

### 1.1 Decide the optimization contract first

Before touching files, pin down with the user, in one short written note:

- **Objective** — what is being improved? (speed, accuracy, memory, a score…)
- **Metric** — the single number the judger rewards, its **direction**
  (minimize or maximize), its **unit**, and the **command that prints it**. This
  command must run from a clean checkout and emit one parseable number. If you
  cannot write a one-line shell command that prints the metric, you do not yet
  have a metric — stop and define one.
- **Improvement threshold** — the minimum delta that counts as an improvement
  (absolute, or relative percent). Below this, a candidate is "no improvement",
  not a win.
- **Validation gates** — the correctness checks every candidate must keep green.
  These are *non-negotiable*: a candidate that improves the metric but breaks a
  gate is rejected. List each as a name + a shell command + a human-readable
  success criterion. Typical gates: a unit-level numerical-drift check, an
  end-to-end behavioral test, a multithread-determinism check, a
  no-regression check on the metric itself.
- **Editable vs frozen paths** — exactly which source files a candidate may
  touch, and which (tests, fixtures, references, build config) are immutable.
  GEPA enforces this per-commit via git audit; an over-broad `editable_paths`
  lets the executor "win" by editing the test. Be conservative.
- **Baseline ref** — the git commit (or ref) the pack is pinned to. The
  baseline's metric value is what every candidate is measured against.

Write this down as a context doc in the pack (see 1.4). If the user cannot
answer any of these, the task is not ready for an autonomous loop — say so and
help them resolve it before building the pack.

### 1.2 The pack directory structure

Create a pack directory (anywhere; conventionally a sibling of the target repo)
and lay it out like this. Every entry is required-for-a-clean-pack unless marked
*optional*; an entry with no purpose for this target may be omitted, but
document the omission in `manifest.json` so a reader knows it was deliberate.

```
<pack-name>-executor-pack/
├── README.md                 # pack overview + baseline pin + external inputs
├── manifest.json             # machine-readable inventory of every pack resource
├── context/                  # docs the executor + proposer read for facts
│   ├── <TASK>_OPTIMIZATION_CONTEXT.md   # the 1.1 contract: metric/gates/safety/baseline
│   ├── SEEDS_<TASK>.md                 # idea ledger: safe patterns, open dirs, retired ideas
│   └── SOURCE_INVENTORY_<TASK>.json     # admitted source files + hot-region notes
├── skills/                    # package-local skills the executor follows
│   └── <pack-name>-opt-flow/SKILL.md     # THE executor playbook (preflight→impl→build→gates→verdict)
│   └── <helper skills, e.g. a dev-guide, a reference-repro skill>/
├── assets/                    # large read-only fixtures that must NOT be committed to the repo
│   └── fixtures/<large-file>
├── repo/                      # the target source repo, cloned at the baseline commit
│   ├── (the project's own source tree, tests, scripts, CMake/pyproject…)
│   ├── CLAUDE.md             # repo-level rules: idempotency, build/test quick-ref, hard rules
│   └── TEMP/                 # mount points for assets bind-mounted at runtime
├── config/                    # *optional* — the task + profile for THIS pack
│   ├── <pack-name>.task.json
│   └── <pack-name>.project.profile.json
└── runs/                      # GEPA run outputs land here (gitignore this)
```

This shape is not arbitrary — it mirrors how GEPA's config resolves resources
(see Part 2): `resources.context_paths` points at `context/`, `resources.skills`
names the skills in `skills/`, `resources.readonly_assets` bind-mounts
`assets/`, and `source.repo_path` points at `repo/`. The structure exists so the
config can reference real files.

### 1.3 Populate each layer

**`repo/` — the target source.** Clone the project at the baseline commit on a
dedicated clean branch (e.g. `<task>-clean`). Verify `git rev-parse HEAD`
matches the pinned commit. Materialize any git-LFS fixtures so they are real
binaries, not pointer stubs — a stub makes the executor fail with cryptic
"bad magic"/"not a valid file" errors long after the build succeeds. Mark
materialized LFS paths `skip-worktree` so `git status` stays focused on source
edits. Keep `build/`, `InstallArea/`, and prior run artifacts OUT of the pack —
they are regenerable and pollute the clean baseline. If the project needs a
build directory, the executor creates it inside its own worktree at run time.

**`context/` — the facts.** These are the documents GEPA inlines into the
proposer and executor prompts (via `resources.context_paths`, truncated to a
per-file cap). Write them for an agent that has never seen the project:

- The **optimization context** restates the 1.1 contract: objective, metric,
  gates, editable/frozen paths, baseline, and — crucially — *which regions of
  the source are hot and which edits are known-safe vs known-unsafe*. An
  executor that does not know the hot region will "optimize" cold code and
  report no gain.
- The **seeds ledger** records prior thinking: safe-pattern prefixes a
  candidate's strategy must match, open directions, and **retired ideas** (things
  tried and refuted, with the reason). Without the retired list the proposer
  re-proposes dead ideas every round.
- The **source inventory** lists the admitted source files (the editable set)
  with one-line notes on what each does and where the time/accuracy goes.

Keep these terse. They are injected into prompts — a 50-page doc gets truncated
and nothing in it reaches the agent.

**`skills/` — the executor playbook.** This is the heart of the pack. Write a
`<pack-name>-opt-flow/SKILL.md` that is the **authority** for executing one
candidate. Model its section order on this skeleton (replace every concrete
line):

```markdown
---
name: <pack-name>-opt-flow
description: Execute one <task> optimization candidate against <baseline-commit>.
---

# <Task> Optimization Flow

Print `[skill: <pack-name>-opt-flow]` before proceeding.

## Package Root
Work from <pack-dir>/ unless the orchestrator gave you an isolated worktree;
then treat the worktree root as the repo root.

Baseline: commit <full-sha>, short <short-sha>, source repo `repo/`.

## Read First
- manifest.json, context/<TASK>_OPTIMIZATION_CONTEXT.md, context/SEEDS_<TASK>.md,
  context/SOURCE_INVENTORY_<TASK>.json, repo/CLAUDE.md

## Resource Preflight
Before editing, verify: HEAD is <baseline> or a descendant; the large fixture is
present and a real binary (not a tiny pointer stub); each external input path
exists. If any fails, DO NOT improvise — return a JSON verdict with
validation.passed=false and failure_categories:["resource_missing"].

## Implementation Rules
- Implement one candidate idea only.
- Edit only <editable globs>. Touch at most N files.
- Do not edit tests/scripts/fixtures/build config/reference outputs.
- Do not regenerate golden reference files. Do not relax tolerances.
- Do not push/fetch/switch branches/create worktrees — the orchestrator owns Git.
- Commit before any gate that keys its output to HEAD.

## Build
Use <shell>. <source env>; <cmake/make/pip/… build commands>.

## Gates (run in this order)
1. <numerical-drift / unit gate>
2. <drift-ratchet gate, if drift is keyed to commits>
3. <metric benchmark, N reps>
4. <end-to-end behavioral gate>
5. <determinism / multithread gate>
Interpretation: <each gate's pass criterion>. Stop the expensive gates as soon
as a cheap one fails.

## JSON Verdict
Always return ONLY a JSON object (schema below), even on partial/failed runs.

{ "summary": "...", "implementation": {"changed_files":[],"commit":null,
  "commands_run":[],"notes":""}, "metrics": {"primary":null,"baseline":null,
  "delta":null,"reps":[]}, "validation": {"passed":false,"checks":[],
  "regressions":[]}, "failure_categories":[], "diagnostics":[],
  "artifact_paths":[], "errors":[] }

validation.passed=true only if ALL gates green AND the candidate beats the
same-machine baseline by >= the improvement threshold.

## Outcome Hygiene
In an isolated worktree, report lessons in the JSON, not by editing pack context.
```

The executor will follow this verbatim. If a gate command is wrong, or a build
step is missing, or the JSON schema is off, candidates fail — so get it right in
Part 1, not during a 6-round run. Add helper skills (a dev-guide, a
reference-fit-repro skill) only if the executor genuinely needs them; do not pad
the pack.

A subtle but critical point: **GEPA does not auto-load `resources.skills` into
the spawned claude.** The skill names in the config surface only as *text* in
the prompt ("Skills: [...]"). For the executor to actually invoke a skill, the
SKILL.md files must live where Claude Code discovers skills relative to the
executor's working directory (the worktree / `repo/`), or in the user's
`~/.claude/skills/`. The pack's `skills/` dir is the canonical source — copy or
symlink them into the repo's `.claude/skills/` (or the user skills dir) so they
are invocable. Confirm this in Part 1's dry-run.

**`assets/` — large read-only fixtures.** Anything too big or too
environment-specific to live in the repo (a multi-GB table, a calibration
file, an input dataset) goes here and is **bind-mounted** into the worktree at
run time via `resources.readonly_assets` (`{source, target}`). The executor
reads it from `target` as if it were in the repo. Never commit these into
`repo/` — a 1 GB file in git makes every worktree operation slow and every
candidate diff noisy.

**`manifest.json` — the inventory.** Write a machine-readable manifest so the
pack is self-describing. Use this shape (every field is a hint to a future
pack-author, including yourself):

```json
{
  "name": "<pack-name>-executor-pack",
  "purpose": "Clean package-local resources for <task> optimization execution.",
  "baseline": {
    "label": "<version label>", "commit": "<full-sha>", "short_commit": "<short>",
    "package_branch": "<task>-clean", "repo_dir": "repo"
  },
  "primary_skill": "skills/<pack-name>-opt-flow/SKILL.md",
  "package_local_skills": ["skills/..."],
  "context": ["context/<TASK>_OPTIMIZATION_CONTEXT.md", "context/SEEDS_<TASK>.md",
              "context/SOURCE_INVENTORY_<TASK>.json"],
  "repo_required_paths": ["<the admitted source dir>", "<the test/fixture dirs>",
                          "<the gate scripts>", "<CMakeLists/pyproject>", "CLAUDE.md"],
  "package_assets": [{"path":"assets/fixtures/<large>","target_in_repo":"<target>","size_note":"..."}],
  "external_runtime_inputs": {"<env setup script>": "<abs path>", "<input>": "<abs path>"},
  "excluded_as_unclean": ["old run dirs","prior worktrees","build/","InstallArea/","..."]
}
```

**`README.md` — the human overview.** State the baseline pin, the package-local
skill, the external read-only inputs still expected on the host, and what is
excluded as unclean. One screen of text.

### 1.4 Dry-run the pack yourself (do NOT skip this)

This is the single most valuable step. Before handing the pack to GEPA, **act
as the executor**: follow your own `<pack-name>-opt-flow/SKILL.md` from
preflight to verdict, by hand, on a no-op candidate (apply a trivially
bit-identical edit, e.g. a comment, so all gates should pass with zero gain).
The goal is to find every gap *before* it costs a 20-minute candidate run.

Run through, in order, exactly as the skill says:

1. **Preflight** — does every resource the skill names actually exist at the
   path the skill expects? Are binaries real (hundreds of KB / GB), not LFS
   stubs? Are external inputs mounted/present?
2. **Environment** — source the env setup script in the *same shell* the
   executor will use. Does it leave every tool the build needs on PATH
   (compiler, cmake/make, python, the test runner)? A setup script that works
   under one shell but leaves a tool unset under another is the most common
   silent failure — verify under the exact shell the executor runs in.
3. **Build** — does the build command produce the binaries the gates need?
4. **Gates** — does each gate command run, exit cleanly on the unmodified
   baseline, and print the value/text the skill claims it will? A gate that
   can't run on the baseline can never pass on a candidate.
5. **Metric** — does the metric command print one parseable number, three times
   in a row, with acceptable variance? Measure the *noise floor* now: a
   benchmark with ±6% run-to-run noise cannot certify a 1% improvement. If the
   noise exceeds the improvement threshold, increase reps or the event/sample
   size until the signal is above the noise.
6. **Verdict JSON** — can you fill the schema end-to-end from real outputs?

Record every failure you hit and fix the *pack* (the skill, the paths, the env,
the manifest), not the workaround. Common dry-run failures and their fixes:

- **LFS pointer stub** → `git lfs pull` (credential-store a token if needed;
  never commit the token) or regenerate the fixture per the project's bootstrap
  procedure.
- **Large fixture missing** → bootstrap it per the project's dump/bootstrap
  path, into `assets/`, then verify the bind-mount target.
- **A dump/bootstrap build is not thread-safe** → use it only for fixture
  capture; rebuild the production variant before any multithread/determinism
  gate.
- **Setup script leaves a tool unset under one shell** → run the executor in the
   shell where it resolves; document the shell requirement in the skill.
- **Metric noise > improvement threshold** → more reps / bigger sample.
- **A gate keys its output to HEAD** → the skill must commit before that gate,
  or the output keys to the wrong commit.

Only when a no-op candidate passes preflight→build→all gates→verdict
(`validation.passed=false, failure_categories:["no_improvement"]` is the correct
no-op outcome — all gates green, zero gain) is the pack ready for GEPA.

---

## Part 2 — Install GEPA and launch the loop

With a dry-runned pack, Part 2 is mechanical. GEPA lives at
`GEPA-AI-researcher/` (the repo containing this skill). It is a `pipx`-installable
CLI whose only hard dependency is PyYAML; the agent backend is the `claude` CLI.

### 2.1 Install

```bash
cd <GEPA-AI-researcher checkout>
pipx install -e .          # or: pip install -e .  (Python ≥3.10)
gepa doctor                # host check: git, python, apptainer, claude
```

Then ensure the **executor backend** is available:

- The `claude` CLI — the executor is a spawned `claude -p` subprocess. Install
  (`curl -fsSL https://claude.ai/install.sh | bash` or
  `npm install -g @anthropic-ai/claude-code`), run `claude doctor` and
  authenticate with a Claude-Code-capable account. If the small/fast sub-task
  model is gateway-routed, export `ANTHROPIC_SMALL_FAST_MODEL` to a value your
  gateway resolves, or `claude -p` returns rc=1.
- **Apptainer (optional, recommended for non-trivial tasks)** — if the task
  needs a toolchain or filesystem the host doesn't have, set
  `runtime_backend: apptainer` (see 2.3). GEPA auto-builds and caches the
  executor image; Docker is not required. Install Apptainer separately or let
  GEPA run a site install hook.

### 2.2 Author the two config files

GEPA uses a strict three-layer config: a **task** file (per experiment), a
**project profile** (once per project), and a resolved **snapshot** GEPA
generates at run time. The canonical complete examples are
`examples/function_discovery/task.yaml` + `project.profile.yaml` (a generic
local task) and `examples/omilrec/` (a containerized C++ task) — read them once,
then write your own. Schema is `schema_version: 1`; unknown fields are **errors
at `gepa validate`**, deliberately, so a misspelled knob fails loud.

**Path resolution rules (get these right or paths silently miss):**
- Paths in the **task** file are relative to the **task file**.
- `project.profile` is a path relative to the **task file** (can be absolute).
- `source.repo_path` is relative to the **profile file** (can be absolute).
- `project.ref` / `source.default_ref` is resolved to a real git SHA via
  `git -C <repo> rev-parse --verify <ref>^{commit}`; for `git_worktree` mode a
  ref is **required**. Pin it to the pack's baseline commit.

**Task file (`<pack>.task.yaml`)** — the experiment:

```yaml
schema_version: 1
kind: task

task:
  name: <unique-task-name>
  goal: <one sentence: improve <metric> while keeping every gate green,
        editing only <editable set>, from <baseline>.>
  samples:                       # optional; names the eval subsets
    - sample_id: <feedback_sample>
      description: Used for the D_feedback minibatch.
    - sample_id: <pareto_sample>
      description: Used for the D_pareto full evaluation.

project:
  profile: <pack>.project.profile.yaml   # path relative to THIS file
  ref: <baseline-sha-or-ref>             # resolved to a real SHA; required for worktrees

metric:
  name: <metric_name>
  direction: minimize           # or maximize
  unit: <unit>
  command: <one shell command that prints the metric number, env-prefixed as needed>
  repeats: <N>                  # >=3 for noisy metrics; raise until signal > noise
  improvement:
    mode: relative_percent      # or absolute
    minimum: <threshold>        # the dry-run noise floor informs this

validation:
  checks:
    - name: <gate-name>
      command: <shell command>
      success_criteria: <human-readable pass condition>
    # ... one entry per gate from the 1.1 contract, in the order the skill runs them

safety:
  editable_paths: [<editable globs>]   # subset of the profile's; cannot broaden it
  frozen_paths: [<immutable globs>]    # tests, fixtures, references, build config
  max_files_per_candidate: <N>
  max_commits_per_candidate: 1

budget:
  max_rounds: <N>              # total mutation rounds
  min_rounds: <N>              # no early stop before this
  patience: <N>                # rounds with no improvement before early stop
  candidates_per_round: <N>

# Optional knobs (defaults shown) — omit any you don't need:
initialization: { seed_count: <N> }      # default = candidates_per_round
generation:     { batch_size: <N>, enable_merge: false }
gepa:
  frontier_policy: pareto
  acceptance_policy: minibatch_improves_then_pareto
  minibatch_size: 1
  parent_sampling: pareto_win_weighted
  feedback_sample_ids: [<from task.samples>]
  pareto_sample_ids: [<from task.samples>]
judger: { pass_threshold: 0.85 }
usage_tracking: { enabled: true, persist_raw_envelope: true,
                  print_round_summary: true, print_run_summary: true }
evidence: { visualize_when_applicable: false, plot_selection_policy: proposer_selects,
            artifact_formats: [], guidance: <one line> }
```

**Project profile (`<pack>.project.profile.yaml`)** — the project, once:

```yaml
schema_version: 1
kind: project_profile
name: <project-name>

source:
  repo_path: <path to repo/, relative to THIS file or absolute>
  default_ref: <baseline-sha-or-ref>
  workspace_mode: git_worktree      # git_worktree (per-candidate worktree + commit audit)
                                    #   or artifact_directory (artifacts only, no source worktree)

environment:                        # v1: the task's environment narrative
  description: <one line: OS, toolchain, how it's provisioned>
  setup_commands:
    - <source /abs/or/profile-relative/setup.sh>
    - <source InstallArea/setup.sh>      # if the build installs an overlay
  python_command: <python>               # absolute if a specific interpreter is needed
  dependency_policy: <e.g. "Use installed packages only; do not install.">

resources:
  data_files: [<input/output files the proposer/executor see, abs or profile-relative>]
  context_paths:                       # docs GEPA inlines into proposer+executor prompts
    - <../pack/context/<TASK>_OPTIMIZATION_CONTEXT.md>
    - <../pack/context/SEEDS_<TASK>.md>
    - <../pack/context/SOURCE_INVENTORY_<TASK>.json>
  skills: [<pack-name>-opt-flow, <helper-skills>]   # names only — text in prompts; NOT auto-loaded
  readonly_assets:                     # large fixtures bind-mounted into the worktree
    - source: <../pack/assets/fixtures/<large>>
      target: <TEMP/fixtures/<large>>
  pre_materialized_lfs_paths: [<repo-relative LFS globs already materialized>]
  generated_tracked_paths: [<benchmarks/*.csv — appended each candidate, tracked>]
  hash_artifacts: [<build/bin/tool, InstallArea/lib/lib.so — fingerprinted for cache>]

agent:
  command: claude
  timeout_seconds: <per-candidate wall-clock cap; >= time for build+all gates>
  extra_args:
    - --permission-mode
    - acceptEdits
    - --allowedTools
    - Read,Edit,Write,Glob,Grep,Bash

execution:
  lifecycle: materialize_once       # materialize_once (reuse across candidates) or stateless
  max_parallel_candidates: <N>       # caps executor worker parallelism
  fail_fast: false
  runtime_backend: local             # local (default) or apptainer
  # apptainer: { ... }               # see 2.3

safety:                              # the project safety ceiling; task may tighten, not broaden
  editable_paths: [<editable globs>]
  frozen_paths: [<immutable globs>]
  max_files_per_candidate: <N>
  max_commits_per_candidate: 1
```

A few non-obvious rules the schema enforces (so you don't learn them from a
failed `validate`):

- `project.profile` and `project.inline` are mutually exclusive. Use `profile`
  for real projects; `inline` only for tiny tests.
- Task `safety.editable_paths` must be a **subset** of the profile's — the task
  can tighten, never broaden. Frozen lists concatenate. `max_files`/`max_commits`
  take the min.
- `budget.patience` resolves to `no_improvement_patience`; `budget.min_rounds`
  blocks early stop before that many rounds; `judger.pass_threshold` enables
  early stop once the best score crosses it (after `min_rounds`).
- `gepa.feedback_sample_ids` / `pareto_sample_ids` select named `task.samples`.
  Omit them and GEPA deterministically splits the configured samples. With one
  sample, both use it.
- Role contracts: the **proposer** sees objective/metric/resources/safety/runtime
  + prior context + feedback; the **executor** adds `validation`; the **judger**
  sees objective/metric/validation only. `agent.*` (command/timeout/extra_args)
  is host-side plumbing and **never** appears in any role prompt — so don't put
  task instructions there.
- Sensitive keys (token/secret/password/credential/api-key) are redacted in
  `config.snapshot.json`; raw agent envelopes land under `usage/raw/` when
  `persist_raw_envelope` is on.

### 2.3 Containerized executor (when the host can't run the task)

If the task needs a toolchain, CVMFS, or a filesystem the host lacks, set
`execution.runtime_backend: apptainer`. GEPA derives the image from your config,
builds/reuses a thin SIF, and validates it by really running commands inside it.
Two common shapes:

```yaml
# Thin auto-materialized image (default). GEPA picks almalinux:9 when your
# commands reference /cvmfs or build tools (gcc/g++/cmake/make), or
# python:3.11-slim for pure-Python tasks. Override with apptainer.base_image.
execution:
  runtime_backend: apptainer
  apptainer:
    auto_image: true
    command: claude
    cleanenv: true
    containall: true
    writable_tmpfs: true
    container_repo: /workspace/repo
    container_artifacts: /workspace/artifacts
    container_scratch: /workspace/scratch
    container_home: /workspace/home
    env_allowlist: []
    readonly_binds: []
    extra_binds: []

# Or your own prebuilt SIF:
#   apptainer: { auto_image: false, image: /abs/path/your.sif }
```

**Two apptainer pitfalls that bite at run time — verify both in the dry-run:**

1. **Bind-mounted external inputs must be visible inside the container.** If a
   `setup_commands` entry or gate command references a host path (a `/cvmfs/...`
   release, a `/data/...` dataset), that path must be bound into the container
   AND present on the host *before* launch. GEPA warns and skips a `/cvmfs` bind
   it can't find — so a setup script that sources `/cvmfs/.../setup.sh` will
   fail with "No such file or directory" inside the container even though it
   works on the host. Source/mount CVMFS (and any `/data` input) on the host
   first, and confirm the bind covers it.
2. **Skills are not auto-loaded by the apptainer backend either.** The same
   text-only rule from 1.3 applies. GEPA projects the host `~/.claude` into the
   container home, so skills installed in the user's `~/.claude/skills/` ride
   along — but `resources.skills` config strings still become only prompt text.
   Place the pack's skills where the executor's cwd discovers them, or in the
   user skills dir, and confirm invocation in the dry-run.

Do **not** bind the host `$HOME` directly; use a minimal `claude_home_template`
project into the per-execution HOME.

### 2.4 Validate, then launch

```bash
# Pure config validation (no run, no worktree). Fix every error before running.
gepa validate --config <pack>.task.yaml

# Inspect what GEPA actually resolved (paths absolute, SHA resolved, defaults):
gepa resolve  --config <pack>.task.yaml
gepa explain  --config <pack>.task.yaml

# If using apptainer, build/reuse + validate the image explicitly first:
gepa doctor          --config <pack>.task.yaml
gepa setup-apptainer --config <pack>.task.yaml
```

`validate` is strict and fast — run it until it's clean. A config that fails
`validate` will fail worse mid-run. Only then launch:

```bash
gepa run --config <pack>.task.yaml --run-dir <pack>/runs/run-001
```

Notes:

- `--run-dir` is optional but strongly recommended; **resume requires the same
  explicit run dir**: `gepa run --config <pack>.task.yaml --run-dir <same> --resume`.
- For a first end-to-end smoke, set a tiny `budget` (1–2 rounds, 1–2 candidates)
  and watch the first candidate's trace under `<run-dir>/traces/`. Confirm from
  the trace that the executor prompt reports the right worktree/repo paths,
  the container (if any) can run the build + gates + metric, generated files
  land under the candidate artifacts/scratch area, and the host-side commit
  audit records the expected worktree diff. A clean smoke at small budget is
  the last dry-run; scale the budget up only after it.
- `gepa` is stdlib-only and not pip-installed into the loop's environment; if
  you launch via a wrapper script rather than the `gepa` console entry, run
  from the `GEPA-AI-researcher/` checkout so `gepa_researcher` is importable,
  or `pipx install -e .` once.

### 2.5 What each run produces

Each `--run-dir` stores: `config.snapshot.json` (the resolved, redacted config),
the dataset split, prior context, candidate pool, execution traces, judger
judgments, the score matrix, the Pareto frontier, usage summaries, and a
`final_report.md`. When a candidate fails, read its trace — the failure
category (`resource_missing`, `environment_failure`, `no_implementation`,
`no_validation`, `no_metrics`, `no_improvement`) tells you whether the pack
(Part 1) or the config (Part 2) is at fault. A run where every candidate dies on
`environment_failure`/`resource_missing` means the dry-run in 1.4 was skipped or
insufficient — go back and re-dry-run, do not raise the budget.

---

## When to use each part

- User says "set up GEPA for <project>" / "prepare a gepa package" / "make my
  repo runnable by gepa" → start at **Part 1**, even if a config already
  exists; an un-dry-runned pack is the usual cause of loop death.
- User says "run gepa" / "launch the loop" / "start the optimization" and a
  dry-runned pack exists → **Part 2** directly.
- User says "gepa candidates keep failing" → read the failure categories from
  the last run's traces; `resource_missing`/`environment_failure` → back to
  Part 1.4; `no_implementation`/`no_validation` → the executor skill (1.3) or
  the validation config (2.2); `no_metrics` → the metric command/noise (1.4 #5).

The reference pack at `omilrec_opt/omilrec-br111-executor-pack/` and the
templates at `GEPA-AI-researcher/examples/omilrec/` are one concrete instance
of this entire skill — read them to see the shape, then generalize.
