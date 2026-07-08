# GEPA-style Research Orchestrator Design

## 1. Goal

This repository starts with a minimal, bounded research loop:

```text
proposer -> executor -> judger -> gater
```

The goal is to validate whether GEPA-style trace-driven iteration can improve a
small task, not to build a full AI Scientist. Version 1 optimizes a paper-QA
answer prompt over a tiny fixed dataset.

Non-goals:

- no infinite autonomous research loop
- no model weight training
- no complex multi-agent runtime
- no tree search yet
- no claim of scientific discovery

## 2. GEPA-style Mapping

GEPA's useful engineering idea is not "use many agents"; it is:

1. run a candidate in a real harness
2. preserve execution and evaluation traces
3. judge with scores plus actionable feedback
4. propose a small mutation
5. gate candidates under a budget

In this skeleton:

- `Candidate` is the evolving prompt/config hypothesis.
- `Trace` is the executor output for each task sample.
- `Judgment` is the metric and feedback signal.
- `Decision` is the gater's keep/reject/stop choice.

## 3. Components

### Proposer

The proposer creates exactly one candidate per round. The current implementation
is deterministic and rule-based so the orchestrator can be tested without an LLM.
Later, replace `RuleBasedProposer.propose()` with an LLM call while preserving
the `Candidate` schema.

### Executor

The executor is intentionally low freedom. It takes a candidate and fixed task
samples, runs the candidate, and saves per-sample traces. The current
`PaperQAExecutor` is a mock harness. Its interface is the important part.

### Judger

The judger returns numeric score and actionable feedback. Version 1 uses hard
checks for correctness, evidence support, and output format. LLM judging can be
added later, but it should not replace hard checks when they exist.

### Gater

The gater manages best-so-far state and stopping rules. Version 1 supports:

- keep if score improves
- reject if score does not improve
- stop at pass threshold
- stop at max rounds
- stop after no-improvement patience

## 4. Main Loop

The loop is bounded on purpose:

```python
state = load_or_initialize_state()

for round_id in range(config.max_rounds):
    candidate = proposer(state)
    trace = executor(candidate, config.task)
    judgment = judger(candidate, trace, config.rubric)
    decision = gater(state, candidate, judgment)

    persist(candidate, trace, judgment, decision)
    state = update_state(state, candidate, judgment, decision)

    if decision.stop:
        break

write_final_report(state)
```

Do not use `while true` in v1. A research loop must be stoppable, auditable, and
budgeted before it becomes more autonomous.

## 5. File Layout

```text
gepa_researcher/
  orchestrator.py
  proposer.py
  executor.py
  judger.py
  gater.py
  schemas.py
  io_utils.py
examples/paper_qa/
  config.json
  runs/
prompts/
  proposer.md
  judger.md
  gater.md
docs/
  orchestrator_design.md
```

Each run writes:

```text
runs/<timestamp>/
  config.snapshot.json
  state.json
  candidates.jsonl
  judgments.jsonl
  decisions.jsonl
  traces/round_000/
    candidate.json
    trace.json
    judgment.json
    decision.json
  final_report.md
```

## 6. First Experiment

Task: optimize a paper-QA answer prompt.

Metrics:

- answer correctness
- evidence support
- format compliance
- no hallucination when evidence is absent

Budget:

- max 3 rounds
- one candidate per round
- fixed small dataset

## 7. Extension Path

1. Replace rule-based proposer with LLM proposer.
2. Replace mock executor with real paper-QA model calls.
3. Add a candidate pool and best-so-far archive.
4. Add Pareto gater for multiple objectives.
5. Add parallel candidates per round.
6. Split executor into literature, code, and analysis tools.
7. Convert the stabilized workflow into a Codex skill.
8. Add tree search and long-term memory.
