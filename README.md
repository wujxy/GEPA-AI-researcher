# GEPA-AI-researcher

Minimal GEPA-style research loop skeleton.

This repository starts with a bounded, auditable orchestrator for a small
paper-QA prompt optimization task. It is intentionally not a full AI Scientist,
not a multi-agent framework, and not an infinite autonomous loop.

The initial loop is:

```text
proposer -> executor -> judger -> gater
```

Run the smoke demo from WSL:

```bash
cd /home/yoru/AI-sci/GEPA-AI-researcher
python -m gepa_researcher.orchestrator --config examples/paper_qa/config.json
```

Outputs are written under `examples/paper_qa/runs/`.

Run the standard-library smoke test:

```bash
python -m unittest discover -s tests -q
```

Run the full Claude Code agent loop after installing and authenticating
Claude Code:

```bash
python3 -m gepa_researcher.orchestrator --config examples/function_discovery/config.claude.json
```

This uses actual Claude Code calls for proposer, executor, judger, and gater.
