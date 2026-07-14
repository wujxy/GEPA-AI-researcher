# Tiny Algorithm GEPA Real-Chain Example

This example is a lightweight real-environment GEPA task. It is intentionally
small, but it exercises the same outer loop shape as a heavier project:

```text
AgentProposer -> AgentExecutor -> Git commit -> feedback/pareto eval -> AgentJudger -> GEPA gate
```

The target repo lives in `repo/` and is a real Git working tree. The task asks
GEPA to optimize a correct but slow pair-counting function.

Run from the GEPA checkout:

```bash
python -m gepa_researcher.cli validate --config examples/tiny_algo_opt/task.yaml --no-materialize
python -m gepa_researcher.cli run --config examples/tiny_algo_opt/task.yaml --run-dir examples/tiny_algo_opt/runs/run-<id>
```

The benchmark and drift checks write tracked files under `repo/benchmarks/`.
Those files are declared as `generated_tracked_paths` in the project profile so
evaluate-only executions can produce task evidence without being mistaken for
source mutations.
