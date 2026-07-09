from __future__ import annotations

import argparse
from pathlib import Path

from .manager import ResearchManager


def main() -> None:
    parser = argparse.ArgumentParser(description="GEPA-AI-researcher CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Create a config through a terminal guide.")
    init.add_argument("--out", default="gepa_config.json", help="Output config path.")
    chat = sub.add_parser("chat", help="Alias for init.")
    chat.add_argument("--out", default="gepa_config.json", help="Output config path.")
    run = sub.add_parser("run", help="Run an existing config.")
    run.add_argument("--config", required=True, help="Config path.")
    args = parser.parse_args()
    manager = ResearchManager()
    if args.command in {"init", "chat"}:
        out = Path(args.out).expanduser().resolve()
        manager.init_config(out)
        print(f"Config written: {out}")
        print(f"Run with: python -m gepa_researcher.cli run --config {out}")
        return
    state = manager.run_config(Path(args.config).expanduser().resolve())
    print(f"Run complete. Best={state.best_candidate_id} score={state.best_score:.4f}")


if __name__ == "__main__":
    main()
