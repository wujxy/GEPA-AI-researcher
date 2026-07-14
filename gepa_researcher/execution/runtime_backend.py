from __future__ import annotations

import os
import shlex
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..domain.execution import ExecutionRecord, ExecutionSpec
from .sandbox import SandboxSession


class RuntimeBackendError(RuntimeError):
    pass


@dataclass
class RuntimeLease:
    backend: str
    repo_path: str
    artifact_path: str
    host_cwd: str | None
    command: str | None = None
    command_prefix: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    inherit_host_env: bool = True
    artifacts: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        # Security: Redact environment variable values while preserving keys for debugging
        if "env" in data and isinstance(data["env"], dict):
            env_keys = list(data["env"].keys())
            data["env"] = {
                "_keys": env_keys,
                "_count": len(env_keys),
                "_redacted": True,
                "_note": "Environment variable values redacted for security"
            }
        return data


class LocalRuntimeBackend:
    name = "local"

    def __init__(self, run_dir: Path, config: dict[str, Any]):
        self.run_dir = run_dir
        self.config = config

    def prepare(
        self,
        spec: ExecutionSpec,
        session: SandboxSession,
        record: ExecutionRecord,
    ) -> RuntimeLease:
        return RuntimeLease(
            backend=self.name,
            repo_path=str(session.repo_path),
            artifact_path=str(session.artifact_path),
            host_cwd=str(session.repo_path),
            env={
                "GEPA_CANDIDATE_ID": spec.candidate_id,
                "GEPA_EXECUTION_ID": record.execution_id,
                "GEPA_INPUT_REVISION": spec.input_revision,
                "GEPA_WORKTREE": str(session.repo_path),
                "GEPA_ARTIFACTS": str(session.artifact_path),
            },
            inherit_host_env=True,
        )


class ApptainerRuntimeBackend:
    name = "apptainer"

    def __init__(self, run_dir: Path, config: dict[str, Any]):
        self.run_dir = run_dir
        self.config = config
        self.runtime_spec = dict(config.get("_runtime_spec") or config.get("_runtime_ir") or {})
        self.executor_config = dict(config.get("executor") or {})
        if not self.runtime_spec:
            raise RuntimeBackendError("_runtime_spec is required for apptainer runtime")
        self.apptainer_config = dict(self.runtime_spec.get("apptainer") or {})
        self.container_repo = str(self.runtime_spec.get("workdir", "/workspace/repo"))
        self.container_artifacts = str(self.apptainer_config.get("container_artifacts", "/workspace/artifacts"))
        self.container_scratch = str(self.apptainer_config.get("container_scratch", "/workspace/scratch"))
        self.container_home = str(self.apptainer_config.get("container_home", "/workspace/home"))

    def prepare(
        self,
        spec: ExecutionSpec,
        session: SandboxSession,
        record: ExecutionRecord,
    ) -> RuntimeLease:
        image = self._required_image()
        if self._is_local_image(image) and not Path(image).expanduser().exists():
            raise RuntimeBackendError(f"isolation.image resolved path does not exist: {image}")
        apptainer = str(self.apptainer_config.get("executable", "apptainer"))
        if shutil.which(apptainer) is None and not Path(apptainer).expanduser().exists():
            raise RuntimeBackendError(f"apptainer executable was not found: {apptainer}")

        host_artifacts = Path(session.artifact_path).expanduser().resolve()
        host_repo = Path(session.repo_path).expanduser().resolve()
        host_scratch = Path(session.scratch_path).expanduser().resolve()

        # Per-execution directory structure using execution_id
        execution_id = record.execution_id
        host_home = host_artifacts / f"home_{execution_id}"
        host_tmp = host_scratch / "tmp"

        for path in (host_artifacts, host_scratch, host_home, host_tmp):
            path.mkdir(parents=True, exist_ok=True)
        self._copy_home_template(host_home)
        self._init_claude_home(host_home)

        prefix = [apptainer, "exec"]
        if bool(self.apptainer_config.get("cleanenv", False)):
            prefix.append("--cleanenv")
        if bool(self.apptainer_config.get("containall", False)):
            prefix.append("--containall")
        if bool(self.apptainer_config.get("writable_tmpfs", True)):
            prefix.append("--writable-tmpfs")
        if bool(self.apptainer_config.get("userns", True)):
            prefix.append("--userns")
        prefix.extend(str(a) for a in self.apptainer_config.get("extra_exec_args", []) or [])
        # Update container paths for per-execution structure
        container_scratch = self.container_scratch
        container_home = f"{self.container_artifacts}/home_{execution_id}"

        home_readonly_binds = self._home_readonly_binds(host_home, container_home)

        # --home both bind-mounts host_home->container_home AND sets HOME=container_home
        # inside the container. (Passing HOME via --env is silently ignored by apptainer.)
        prefix.extend(["--home", f"{host_home}:{container_home}"])
        prefix.extend(["--pwd", self.container_repo])
        for bind in self._binds(host_repo, host_artifacts, host_scratch, home_readonly_binds):
            prefix.extend(["--bind", bind])

        # Build environment variables for --env passing
        env = self._build_environment(spec, record, container_scratch)

        # Add environment variables via --env mechanism (Apptainer best practice).
        # subprocess passes argv directly, so values must not be shell-quoted here.
        for key, value in env.items():
            prefix.extend(["--env", f"{key}={value}"])

        prefix.append(image)

        executor_cmd = str(self.runtime_spec.get("command", self.config.get("agent", {}).get("command", "claude")))
        runtime_init = {"init": list(self.runtime_spec.get("setup") or self.runtime_spec.get("init") or []), "preflight": list(self.runtime_spec.get("check") or self.runtime_spec.get("preflight") or [])}
        inline_shell = self._runtime_shell(runtime_init) if (runtime_init["init"] or runtime_init["preflight"]) else ""
        entrypoint_host: Path | None = None
        entrypoint_container: str | None = None
        if inline_shell:
            entrypoint_host = host_artifacts / f"gepa-runtime-entrypoint-{execution_id}.sh"
            entrypoint_host.write_text(inline_shell + "\n", encoding="utf-8")
            entrypoint_host.chmod(0o755)
            entrypoint_container = f"{self.container_artifacts}/gepa-runtime-entrypoint-{execution_id}.sh"
            prefix.extend(["/usr/bin/env", "bash", entrypoint_container])

        return RuntimeLease(
            backend=self.name,
            repo_path=self.container_repo,
            artifact_path=self.container_artifacts,
            host_cwd=str(host_repo),
            command=executor_cmd,
            command_prefix=prefix,
            env={},  # Empty since environment variables passed via --env
            inherit_host_env=False,
            artifacts={
                "host_repo": str(host_repo),
                "host_artifacts": str(host_artifacts),
                "host_scratch": str(host_scratch),
                "host_home": str(host_home),
                "executor_command": executor_cmd,
                "runtime_init": runtime_init,
                "runtime_shell": inline_shell,
                "runtime_entrypoint_host": str(entrypoint_host) if entrypoint_host else None,
                "runtime_entrypoint_container": entrypoint_container,
            },
        )

    def _required_image(self) -> str:
        value = self.runtime_spec.get("image") or self.apptainer_config.get("image")
        if not isinstance(value, str) or not value.strip():
            raise RuntimeBackendError("isolation.image: required non-empty string after Apptainer materialization")
        return str(Path(value).expanduser()) if self._is_local_image(value) else str(value)

    def _is_local_image(self, value: str) -> bool:
        return not (value.startswith("docker://") or value.startswith("library://") or value.startswith("oras://"))

    def _copy_home_template(self, host_home: Path) -> None:
        template = self.apptainer_config.get("claude_home_template")
        if not template:
            return
        source = Path(str(template)).expanduser().resolve()
        if not source.exists():
            raise RuntimeBackendError(f"_runtime_spec.apptainer.claude_home_template does not exist: {source}")
        if source.is_file():
            raise RuntimeBackendError("_runtime_spec.apptainer.claude_home_template must be a directory")
        shutil.copytree(source, host_home, dirs_exist_ok=True)

    def _binds(
        self,
        host_repo: Path,
        host_artifacts: Path,
        host_scratch: Path,
        home_readonly_binds: list[str],
    ) -> list[str]:
        # NOTE: host_home->container_home is intentionally NOT bound here; it is
        # established by the ``--home`` flag in prepare() (which also sets HOME).
        binds = [
            f"{host_repo}:{self.container_repo}",
            f"{host_artifacts}:{self.container_artifacts}",
            f"{host_scratch}:{self.container_scratch}",
        ]
        git_common_dir = self._git_common_dir_for_worktree(host_repo)
        if git_common_dir is not None:
            binds.append(f"{git_common_dir}:{git_common_dir}:rw")
        for item in self.runtime_spec.get("mounts", []) or []:
            source = Path(str(item["source"])).expanduser().resolve()
            target = str(item["target"])
            mode = str(item.get("mode") or "ro")
            binds.append(f"{source}:{target}:{mode}")
        binds.extend(home_readonly_binds)
        binds.extend(self._configured_binds("readonly_binds", readonly=True))
        binds.extend(self._configured_binds("extra_binds", readonly=False))
        return self._dedupe_binds_by_target(binds)

    def _git_common_dir_for_worktree(self, host_repo: Path) -> Path | None:
        git_file = host_repo / ".git"
        if not git_file.is_file():
            return None
        try:
            first_line = git_file.read_text(encoding="utf-8").splitlines()[0].strip()
        except (OSError, IndexError, UnicodeDecodeError):
            return None
        prefix = "gitdir:"
        if not first_line.lower().startswith(prefix):
            return None
        gitdir_text = first_line[len(prefix):].strip()
        gitdir = Path(gitdir_text)
        if not gitdir.is_absolute():
            gitdir = (host_repo / gitdir).resolve()
        else:
            gitdir = gitdir.expanduser().resolve()
        try:
            common_text = (gitdir / "commondir").read_text(encoding="utf-8").splitlines()[0].strip()
        except (OSError, IndexError, UnicodeDecodeError):
            common_dir = gitdir.parent.parent if gitdir.parent.name == "worktrees" else gitdir
        else:
            common_path = Path(common_text)
            common_dir = (gitdir / common_path).resolve() if not common_path.is_absolute() else common_path.expanduser().resolve()
        return common_dir if common_dir.exists() else None

    def _init_claude_home(self, host_home: Path) -> None:
        if not self.apptainer_config.get("auto_init_claude_home", True):
            return
        source_home = Path(str(self.apptainer_config.get("claude_host_home") or Path.home())).expanduser()
        claude_json = source_home / ".claude.json"
        if claude_json.exists():
            shutil.copy2(claude_json, host_home / ".claude.json")

        source_claude = source_home / ".claude"
        target_claude = host_home / ".claude"
        target_claude.mkdir(parents=True, exist_ok=True)
        if source_claude.exists():
            for child in source_claude.iterdir():
                if self._skip_claude_home_entry(child.name):
                    continue
                target = target_claude / child.name
                if child.is_dir():
                    shutil.copytree(
                        child,
                        target,
                        dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("*.lock", "*.tmp"),
                    )
                elif child.is_file():
                    shutil.copy2(child, target)
        (target_claude / "session-env").mkdir(parents=True, exist_ok=True)

    def _skip_claude_home_entry(self, name: str) -> bool:
        return name in {
            "backups",
            "cache",
            "debug",
            "file-history",
            "history.jsonl",
            "paste-cache",
            "plans",
            "projects",
            "sessions",
            "session-env",
            "shell-snapshots",
            "stats-cache.json",
            "tasks",
        }

    def _home_readonly_binds(self, host_home: Path, container_home: str) -> list[str]:
        configured = list(self.apptainer_config.get("home_readonly_binds", []) or [])
        binds: list[str] = []
        seen: set[tuple[str, str]] = set()
        for item in configured:
            if isinstance(item, str):
                source = Path(item).expanduser().resolve()
                target_rel = source.name
            elif isinstance(item, dict):
                source = Path(str(item["source"])).expanduser().resolve()
                target_rel = str(item.get("target") or source.name)
            else:
                continue
            if not source.exists():
                continue
            target_rel = target_rel.lstrip("/")
            key = (str(source), target_rel)
            if key in seen:
                continue
            seen.add(key)

            target_host = host_home / target_rel
            if source.is_dir():
                target_host.mkdir(parents=True, exist_ok=True)
            else:
                target_host.parent.mkdir(parents=True, exist_ok=True)
                target_host.touch(exist_ok=True)
            target_container = container_home.rstrip("/") + "/" + target_rel
            binds.append(f"{source}:{target_container}:ro")
        return binds

    def _dedupe_binds_by_target(self, binds: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for bind in binds:
            parts = bind.split(":", 2)
            target = parts[1] if len(parts) >= 2 else parts[0]
            if target in seen:
                continue
            seen.add(target)
            result.append(bind)
        return result

    def _configured_binds(self, field_name: str, *, readonly: bool) -> list[str]:
        values = self.apptainer_config.get(field_name, [])
        if values is None:
            return []
        binds: list[str] = []
        for item in values:
            if isinstance(item, str):
                binds.append(item)
                continue
            if isinstance(item, dict):
                source = str(Path(str(item["source"])).expanduser())
                target = str(item["target"])
                mode = str(item.get("mode") or ("ro" if readonly else "rw"))
                binds.append(f"{source}:{target}:{mode}" if mode else f"{source}:{target}")
        return binds

    def _allowed_host_env(self) -> dict[str, str]:
        names = list((self.runtime_spec.get("env") or {}).get("pass") or [])
        return {name: os.environ[name] for name in dict.fromkeys(names) if name in os.environ}

    def _build_environment(self, spec: ExecutionSpec, record: ExecutionRecord, container_scratch: str) -> dict[str, str]:
        env = {
            "GEPA_CANDIDATE_ID": spec.candidate_id,
            "GEPA_EXECUTION_ID": record.execution_id,
            "GEPA_INPUT_REVISION": spec.input_revision,
            "GEPA_WORKTREE": self.container_repo,
            "GEPA_ARTIFACTS": self.container_artifacts,
            "TMPDIR": f"{container_scratch}/tmp",
        }
        env.update({str(key): str(value) for key, value in ((self.runtime_spec.get("env") or {}).get("set") or {}).items()})
        env.update(self._allowed_host_env())
        return env

    def _runtime_shell(self, runtime_init: dict[str, Any]) -> str:
        commands = [
            "set -euo pipefail",
            f"cd {shlex.quote(self.container_repo)}",
        ]
        init_steps = list(runtime_init.get("init") or [])
        if init_steps:
            commands.append('echo "[gepa-runtime] initializing project environment" >&2')
        commands.extend(self._init_step_command(step) for step in init_steps)
        for step in runtime_init.get("preflight") or []:
            command = str(step["command"])
            commands.append(f"echo {shlex.quote('[gepa-runtime] validating: ' + command)} >&2")
            commands.append(command)
        commands.append('exec "$@"')
        return "\n".join(commands)

    def _init_step_command(self, step: dict[str, Any]) -> str:
        op = str(step.get("op"))
        required = bool(step.get("required", True))
        if op == "source":
            path = str(step["path"])
            source_target = path if path.startswith("/") else path
            target_q = shlex.quote(source_target)
            source_command = f"source {target_q}"
            if required:
                return source_command
            message = shlex.quote(f"[gepa-runtime] optional setup skipped: {source_target}")
            return f"if [ -f {target_q} ]; then {source_command}; else echo {message} >&2; fi"
        if op == "shell":
            return str(step["command"])
        raise RuntimeBackendError(f"runtime init op is not supported: {op}")


def runtime_backend_for(config: dict[str, Any], run_dir: Path):
    backend = str(config.get("executor", {}).get("runtime_backend", "local"))
    if backend == "local":
        return LocalRuntimeBackend(run_dir, config)
    if backend == "apptainer":
        return ApptainerRuntimeBackend(run_dir, config)
    raise RuntimeBackendError(f"executor.runtime_backend: unknown backend {backend!r}")
