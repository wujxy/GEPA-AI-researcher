# Function Discovery Example

This example exercises the schema-version-1 GEPA task/profile configuration and
the full agent loop:

```text
AgentProposer -> AgentExecutor -> AgentJudger -> GEPA gate
```

The canonical files are:

- `task.yaml`: complete task config with all task-level knobs shown.
- `project.profile.yaml`: complete project profile with runtime, resources,
  agent backend, execution, and safety ceilings.
- `data/observations.csv`: one-column numeric observations file.

Validate without creating artifacts:

```bash
python -m gepa_researcher.cli validate --config examples/function_discovery/task.yaml
```

Inspect the resolved config:

```bash
python -m gepa_researcher.cli explain --config examples/function_discovery/task.yaml
```

Run after Claude Code is installed and authenticated:

```bash
python -m gepa_researcher.cli run   --config examples/function_discovery/task.yaml   --run-dir examples/function_discovery/runs/<run-id>
```

`config.claude.json` is retained as a legacy single-file config for compatibility
and smoke tests. Prefer `task.yaml` plus `project.profile.yaml` for new runs.
