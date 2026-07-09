from __future__ import annotations

from pathlib import Path
from typing import Any

from .io_utils import read_json, write_json
from .orchestrator import ResearchOrchestrator


class ResearchManager:
    def __init__(self, cwd: Path | None = None):
        self.cwd = (cwd or Path.cwd()).resolve()

    def build_config_interactively(self) -> dict[str, Any]:
        name = self._ask("Task name", "interactive_research_task")
        goal = self._ask("Research goal", "Infer a compact model from the provided data.")
        data_files = self._split_csv(self._ask("Data files, comma separated", ""))
        context_paths = self._split_csv(self._ask("Prior context paths, comma separated", ""))
        notes = self._split_csv(self._ask("Constraints/preferences, comma separated", ""))
        max_rounds = int(self._ask("Max GEPA rounds", "3"))
        batch_size = int(self._ask("Candidates per round", "3"))
        minibatch = int(self._ask("D_feedback minibatch size", "1"))
        pass_threshold = float(self._ask("Pass threshold", "0.85"))
        enable_merge = self._ask("Enable merge? y/N", "n").lower().startswith("y")
        return {
            "resume": False,
            "components": {"mode": "claude_code_agents"},
            "context": {"paths": context_paths, "notes": notes, "skills": []},
            "budget": {"max_rounds": max_rounds, "no_improvement_patience": 2},
            "generation": {"batch_size": batch_size, "enable_merge": enable_merge},
            "gepa": {"frontier_policy": "pareto", "acceptance_policy": "minibatch_improves_then_pareto", "minibatch_size": minibatch, "parent_sampling": "pareto_win_weighted"},
            "executor": {"max_workers": min(batch_size, 3), "executor_timeout_seconds": 900, "fail_fast": False, "per_candidate_workspace": True},
            "judger": {"pass_threshold": pass_threshold},
            "task": {"name": name, "goal": goal, "data_files": data_files},
        }

    def init_config(self, out_path: Path) -> dict[str, Any]:
        config = self.build_config_interactively()
        print("\nConfig summary:")
        print(f"- task: {config['task']['name']}")
        print(f"- goal: {config['task']['goal']}")
        print(f"- rounds: {config['budget']['max_rounds']}, batch: {config['generation']['batch_size']}")
        if not self._ask("Write this config? Y/n", "y").lower().startswith("y"):
            raise SystemExit("Config init cancelled.")
        write_json(out_path, config)
        return config

    def run_config(self, config_path: Path):
        config = read_json(config_path)
        return ResearchOrchestrator(config=config, config_path=config_path).run()

    def _ask(self, label: str, default: str) -> str:
        suffix = f" [{default}]" if default else ""
        value = input(f"{label}{suffix}: ").strip()
        return value or default

    def _split_csv(self, value: str) -> list[str]:
        return [item.strip() for item in value.split(",") if item.strip()]
