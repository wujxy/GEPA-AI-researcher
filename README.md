# GEPA-AI-researcher

Minimal GEPA-style research loop skeleton.

This repository starts with a bounded, auditable orchestrator for a small
paper-QA prompt optimization task. It is intentionally not a full AI Scientist,
not a multi-agent framework, and not an infinite autonomous loop.

The current GEPA loop is:

```text
Prior Context + Human Goal
  -> Seed Candidate(s)
  -> Initial D_pareto Eval -> Score Matrix
  -> Pareto Weighted Parent Selection
  -> Proposer reflective mutation / optional merge
  -> D_feedback minibatch eval
  -> Gate: improve parent?
  -> D_pareto full eval
  -> Candidate Pool + Score Matrix
```

`score_matrix.json` is only updated from `D_pareto` judgments. `D_feedback`
rollouts are archived as round artifacts and used for reflective mutation and
child-vs-parent gating.

Run the smoke demo from WSL:

```bash
cd /home/yoru/AI-sci/GEPA-AI-researcher
python -m gepa_researcher.orchestrator --config examples/paper_qa/config.json
```

Create a config through the terminal guide, then run it:

```bash
python -m gepa_researcher.cli init --out my_gepa_config.json
python -m gepa_researcher.cli run --config my_gepa_config.json
```

The `chat` subcommand is an alias for `init` in this first version.

Outputs are written under `examples/paper_qa/runs/`.

Run the standard-library tests:

```bash
python3 -m unittest discover -s tests -q
```

Run the full Claude Code agent loop after installing and authenticating
Claude Code:

```bash
python3 -m gepa_researcher.orchestrator --config examples/function_discovery/config.claude.json
```

This uses actual Claude Code calls for proposer, executor, and judger. The GEPA
gate is deterministic: it maintains the candidate pool, score matrix, Pareto
frontier, and accepted/discarded archive.

## GEPA fields

Useful config additions:

- `context.paths`, `context.notes`, `context.skills`: prior material for agents; never used as score matrix values.
- `initialization.seed_count`: number of seed candidates evaluated on `D_pareto` before the loop.
- `gepa.feedback_sample_ids` and `gepa.pareto_sample_ids`: explicit split; otherwise a deterministic split is used.
- `gepa.minibatch_size`: number of `D_feedback` samples per mutation round.
- `gepa.parent_sampling`: defaults to `pareto_win_weighted`.

