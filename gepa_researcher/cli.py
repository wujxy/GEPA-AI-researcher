from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import ConfigError, explain_config, load_and_resolve, sanitize_snapshot
from .execution.container_image import MaterializationError, finalize_runtime, last_materialization
from .storage.io_utils import write_json
from .orchestrator import ResearchOrchestrator


def _add_config_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", required=True, help="Task YAML/JSON or legacy JSON config.")


def _should_materialize(args: argparse.Namespace) -> bool:
    """Whether auto image-materialization may run for this subcommand.

    run/validate materialize by default (opt out with --no-materialize);
    resolve/explain are pure inspection tools and only materialize with --materialize;
    setup-apptainer owns its own materialization in _setup_apptainer.
    """
    command = getattr(args, "command", None)
    if command in ("run", "validate"):
        return not getattr(args, "no_materialize", False)
    if command in ("resolve", "explain"):
        return getattr(args, "materialize", False)
    return False


def _resolve(args: argparse.Namespace) -> tuple[Path, dict]:
    config_path = Path(args.config).expanduser().resolve()
    run_dir = Path(args.run_dir) if getattr(args, "run_dir", None) else None
    resolved = load_and_resolve(
        config_path,
        run_dir=run_dir,
        resume=bool(getattr(args, "resume", False)),
    )
    # Lazy Apptainer image materialization: derives requirements from the resolved
    # config, builds/reuses a thin SIF, and merges image path + binds + userns.
    if (
        _should_materialize(args)
        and resolved.get("executor", {}).get("runtime_backend") == "apptainer"
    ):
        resolved = finalize_runtime(resolved, allow_materialize=True)
    return config_path, resolved


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


def _setup_apptainer(args: argparse.Namespace) -> int:
    config_path, config = _resolve(args)
    exec_cfg = config.get("executor", {})
    if exec_cfg.get("runtime_backend") != "apptainer":
        print("execution.runtime_backend is not 'apptainer'; nothing to materialize.")
        return 0
    if getattr(args, "no_materialize", False):
        print(json.dumps(last_materialization(config) or {}, ensure_ascii=False, indent=2))
        return 0
    from .execution.container_image import _probe_host_runtime, materialize_executor_image
    apptainer_exe = str(exec_cfg.get("apptainer", {}).get("executable") or "apptainer")
    host_probe = _probe_host_runtime(apptainer_exe)
    mat = materialize_executor_image(config, force=bool(args.force), host_probe=host_probe)
    # Reflect the materialized image back into the resolved config view.
    config["executor"]["apptainer"]["image"] = mat.sif_path
    print(f"Apptainer executor image ready: {mat.sif_path}")
    print(json.dumps(mat.diagnostics, ensure_ascii=False, indent=2))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="GEPA-AI-researcher CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="Resolve configuration and run the loop.")
    _add_config_argument(run)
    run.add_argument("--run-dir")
    run.add_argument("--resume", action="store_true")
    run.add_argument(
        "--no-materialize",
        action="store_true",
        help="Do not auto-build/reuse the Apptainer executor image (schema-only resolve).",
    )

    validate = subparsers.add_parser("validate", help="Validate and resolve without creating a run.")
    _add_config_argument(validate)
    validate.add_argument(
        "--no-materialize",
        action="store_true",
        help="Do not auto-build/reuse the Apptainer executor image (schema-only validate).",
    )

    resolve = subparsers.add_parser("resolve", help="Print or save the resolved configuration.")
    _add_config_argument(resolve)
    resolve.add_argument("--out")
    resolve.add_argument(
        "--materialize",
        action="store_true",
        help="Also auto-build/reuse the Apptainer executor image before printing.",
    )

    explain = subparsers.add_parser("explain", help="Explain configuration sources and defaults.")
    _add_config_argument(explain)
    explain.add_argument(
        "--materialize",
        action="store_true",
        help="Also auto-build/reuse the Apptainer executor image before explaining.",
    )

    setup = subparsers.add_parser(
        "setup-apptainer",
        help="Materialize (build/reuse + validate) the Apptainer executor image and print diagnostics.",
    )
    _add_config_argument(setup)
    setup.add_argument("--force", action="store_true", help="Rebuild even if a cached SIF exists.")
    setup.add_argument(
        "--no-materialize",
        action="store_true",
        help="Do not build; only probe the host and report what would be materialized.",
    )
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
        elif args.command == "setup-apptainer":
            code = _setup_apptainer(args)
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
    except MaterializationError as exc:
        print(f"Image materialization failed:\n{exc}", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
