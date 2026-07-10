from __future__ import annotations

import argparse
from pathlib import Path

from .orchestrator import ResearchOrchestrator
from .io_utils import read_json


def main() -> None:
    parser = argparse.ArgumentParser(description="GEPA-AI-researcher CLI")
    parser.add_argument("--config", required=True, help="Path to JSON config file.")
    args = parser.parse_args()

    config_path = Path(args.config).expanduser().resolve()
    config = read_json(config_path)

    orchestrator = ResearchOrchestrator(config=config, config_path=config_path)
    try:
        state = orchestrator.run()
        print(f"Run complete. Best={state.best_candidate_id} score={state.best_score:.4f}")
        print(f"Artifacts: {orchestrator.run_dir}")
    except Exception as exc:
        print(f"Error: {exc}")
        print(f"Artifacts, if any: {orchestrator.run_dir}")
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()