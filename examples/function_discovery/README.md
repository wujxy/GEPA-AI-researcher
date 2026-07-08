# Function Discovery Task

This task is meant to exercise the full Claude Code agent loop:

```text
AgentProposer -> AgentExecutor -> AgentJudger -> AgentGater
```

The agents receive only:

- a one-column numeric observations file
- the goal of finding a compact descriptive mathematical/statistical model
- prior loop traces and feedback

Run after Claude Code is installed and authenticated:

```bash
cd /home/yoru/AI-sci/GEPA-AI-researcher
python3 -m gepa_researcher.orchestrator --config examples/function_discovery/config.claude.json
```

Artifacts will be written under `examples/function_discovery/runs/`.
