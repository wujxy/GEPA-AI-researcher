# Claude Code Setup for Agent Loop

The full agent-backed loop requires the `claude` CLI inside WSL.

Official install options include:

```bash
curl -fsSL https://claude.ai/install.sh | bash
```

or:

```bash
npm install -g @anthropic-ai/claude-code
```

Then verify and authenticate:

```bash
claude --version
claude doctor
claude
```

Claude Code requires a paid Claude Code-capable account or a Console/API-backed
setup. After authentication, run:

```bash
cd /home/yoru/AI-sci/GEPA-AI-researcher
python3 -m gepa_researcher.orchestrator --config examples/function_discovery/config.claude.json
```

This config calls Claude Code in print mode (`claude -p`) for all four roles:

- proposer
- executor
- judger
- gater

The task gives agents only the observed numeric data file and the modeling goal.
