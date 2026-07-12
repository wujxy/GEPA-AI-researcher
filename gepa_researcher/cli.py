from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import ConfigError, explain_config, load_and_resolve, sanitize_snapshot
from .io_utils import write_json
from .orchestrator import ResearchOrchestrator


def _add_config_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", required=True, help="Task YAML/JSON or legacy JSON config.")


def _resolve(args: argparse.Namespace) -> tuple[Path, dict]:
    config_path = Path(args.config).expanduser().resolve()
    run_dir = Path(args.run_dir) if getattr(args, "run_dir", None) else None
    return config_path, load_and_resolve(
        config_path,
        run_dir=run_dir,
        resume=bool(getattr(args, "resume", False)),
    )


def _run(args: argparse.Namespace) -> int:
    config_path, config = _resolve(args)
    for warning in config.get("_meta", {}).get("warnings", []):
        print(f"Config warning: {warning}", file=sys.stderr)
    orchestrator = ResearchOrchestrator(config=config, config_path=config_path)
    try:
        state = orchestrator.run()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        print(f"Artifacts, if any: {orchestrator.run_dir}", file=sys.stderr)
        return 2
    print(f"Run complete. Best={state.best_candidate_id} score={state.best_score:.4f}")
    print(f"Artifacts: {orchestrator.run_dir}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="GEPA-AI-researcher CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="Resolve configuration and run the loop.")
    _add_config_argument(run)
    run.add_argument("--run-dir")
    run.add_argument("--resume", action="store_true")

    validate = subparsers.add_parser("validate", help="Validate and resolve without creating a run.")
    _add_config_argument(validate)

    resolve = subparsers.add_parser("resolve", help="Print or save the resolved configuration.")
    _add_config_argument(resolve)
    resolve.add_argument("--out")

    explain = subparsers.add_parser("explain", help="Explain configuration sources and defaults.")
    _add_config_argument(explain)
    return parser


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0].startswith("-"):
        print("Compatibility CLI syntax detected; prefer the 'run' subcommand.", file=sys.stderr)
        argv.insert(0, "run")
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            code = _run(args)
        else:
            config_path, config = _resolve(args)
            if args.command == "validate":
                print(f"Valid configuration: {config_path}")
                for warning in config.get("_meta", {}).get("warnings", []):
                    print(f"Warning: {warning}")
            elif args.command == "resolve":
                payload = sanitize_snapshot(config)
                if args.out:
                    write_json(Path(args.out).expanduser().resolve(), payload)
                else:
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(explain_config(config))
            code = 0
    except ConfigError as exc:
        parser.error(str(exc))
        return
    raise SystemExit(code)


if __name__ == "__main__":
    main()
