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
    command = getattr(args, "command", None)
    if command in ("run", "validate"):
        return not bool(getattr(args, "no_materialize", False))
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


def _doctor(args: argparse.Namespace) -> int:
    from .execution.container_image import doctor_runtime

    config_path: Path | None = None
    config: dict = {}
    apptainer_cfg: dict = {}
    agent_command = "claude"
    runtime_backend = "unknown"
    if getattr(args, "config", None):
        config_path = Path(args.config).expanduser().resolve()
        config = load_and_resolve(config_path)
        executor = config.get("executor") or {}
        runtime_backend = str(executor.get("runtime_backend", "local"))
        apptainer_cfg = dict(executor.get("apptainer") or {})
        agent_command = str((config.get("agent") or {}).get("command") or "claude")

    check_apptainer = not (config_path is not None and runtime_backend != "apptainer")
    report = doctor_runtime(
        apptainer_cfg,
        agent_command=agent_command,
        allow_install=bool(getattr(args, "install", False)),
        probe=not bool(getattr(args, "no_probe", False)),
        check_apptainer=check_apptainer,
    )
    report["config"] = {
        "path": str(config_path) if config_path else None,
        "runtime_backend": runtime_backend,
        "agent_command": agent_command,
    }

    if getattr(args, "json", False):
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_doctor_report(report)

    if runtime_backend == "local" and config_path is not None:
        return 0
    return 0 if report.get("ok") else 1


def _mark(ok: bool) -> str:
    return "OK" if ok else "WARN"


def _print_doctor_report(report: dict) -> None:
    cfg = report.get("config") or {}
    appt = report.get("apptainer") or {}
    discovery = appt.get("discovery") or {}
    host_probe = appt.get("host_probe") or {}
    claude = report.get("claude") or {}
    auth = claude.get("auth") or {}

    print("GEPA doctor")
    if cfg.get("path"):
        print(f"Config: {cfg['path']}")
        print(f"Runtime backend: {cfg.get('runtime_backend')}")
    else:
        print("Config: <none> (host checks only)")

    print(f"Apptainer: {_mark(bool(appt.get('ok')))}")
    if discovery.get("executable"):
        print(f"  executable: {discovery.get('executable')} ({discovery.get('source')})")
    else:
        print("  executable: not found")
    if discovery.get("install_attempted"):
        print(f"  install attempted: {_mark(bool(discovery.get('install_ok')))}")
        if discovery.get("install_error"):
            print(f"  install error: {discovery.get('install_error')}")
    if host_probe:
        print(f"  version: {host_probe.get('version') or '<unknown>'}")
        print(f"  default exec: {_mark(bool(host_probe.get('default_exec_ok')))}")
        print(f"  userns exec: {_mark(bool(host_probe.get('userns_exec_ok')))}")
        print(f"  will use --userns: {bool(host_probe.get('userns'))}")

    print(f"Claude Code: {_mark(bool(claude.get('ok')))}")
    bind = claude.get("bind") or {}
    if bind.get("claude_bin"):
        print(f"  claude: {bind.get('claude_bin')}")
    else:
        print("  claude: not resolved")
    print(f"Claude auth: {_mark(bool(auth.get('ok')))}")
    if auth.get("host_paths"):
        print(f"  host auth paths: {', '.join(auth.get('host_paths'))}")
    if auth.get("env_keys"):
        print(f"  auth env keys: {', '.join(auth.get('env_keys'))}")

    recommendations = report.get("recommendations") or []
    if recommendations:
        print("Recommendations:")
        for item in recommendations:
            print(f"  - {item}")


def _setup_apptainer(args: argparse.Namespace) -> int:
    config_path, config = _resolve(args)
    exec_cfg = config.get("executor", {})
    runtime_spec = config.get("_runtime_spec") or {}
    if exec_cfg.get("runtime_backend") != "apptainer":
        print("execution.runtime_backend is not 'apptainer'; nothing to materialize.")
        return 0
    apptainer_cfg = dict(runtime_spec.get("apptainer") or {})
    if getattr(args, "no_materialize", False):
        from .execution.container_image import doctor_runtime
        report = doctor_runtime(
            apptainer_cfg,
            agent_command=str(runtime_spec.get("command") or (config.get("agent") or {}).get("command") or "claude"),
            allow_install=bool(getattr(args, "install", False)),
            check_apptainer=True,
        )
        report["materialization"] = last_materialization(config) or {}
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report.get("ok") else 1
    from .execution.container_image import _probe_host_runtime, ensure_apptainer, materialize_executor_image
    discovery = ensure_apptainer(apptainer_cfg, allow_install=bool(getattr(args, "install", False)))
    apptainer_exe = str(discovery.executable)
    runtime_spec.setdefault("apptainer", {})["executable"] = apptainer_exe
    host_probe = _probe_host_runtime(apptainer_exe)
    mat = materialize_executor_image(config, force=bool(args.force), host_probe=host_probe)
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
        help="Skip Apptainer executor image materialization before starting the loop.",
    )

    validate = subparsers.add_parser("validate", help="Validate and resolve without creating a run.")
    _add_config_argument(validate)
    validate.add_argument(
        "--no-materialize",
        action="store_true",
        help="Skip Apptainer executor image materialization during validation.",
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

    doctor = subparsers.add_parser("doctor", help="Check GEPA host runtime, Apptainer, Claude Code, and auth.")
    doctor.add_argument("--config", help="Optional task YAML/JSON to check project-specific runtime settings.")
    doctor.add_argument(
        "--install",
        action="store_true",
        help="Run a configured Apptainer install hook if Apptainer is missing.",
    )
    doctor.add_argument(
        "--no-probe",
        action="store_true",
        help="Only discover executables; do not run Apptainer exec smoke probes.",
    )
    doctor.add_argument("--json", action="store_true", help="Print machine-readable diagnostics.")

    setup = subparsers.add_parser(
        "setup-apptainer",
        help="Materialize (build/reuse + validate) the Apptainer executor image and print diagnostics.",
    )
    _add_config_argument(setup)
    setup.add_argument("--force", action="store_true", help="Rebuild even if a cached SIF exists.")
    setup.add_argument(
        "--install",
        action="store_true",
        help="Run a configured Apptainer install hook if Apptainer is missing before materializing.",
    )
    setup.add_argument(
        "--no-materialize",
        action="store_true",
        help="Do not build; only probe the host and report what would be materialized.",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            code = _run(args)
        elif args.command == "doctor":
            code = _doctor(args)
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
        print(f"Runtime setup failed:\n{exc}", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
