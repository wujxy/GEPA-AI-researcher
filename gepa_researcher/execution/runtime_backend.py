from __future__ import annotations

import os
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..models.schemas import Candidate, ExecutionRecord, WorkspaceLease


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
        candidate: Candidate,
        lease: WorkspaceLease,
        record: ExecutionRecord,
    ) -> RuntimeLease:
        return RuntimeLease(
            backend=self.name,
            repo_path=lease.worktree_path,
            artifact_path=lease.artifact_path,
            host_cwd=lease.worktree_path,
            env={
                "GEPA_CANDIDATE_ID": candidate.candidate_id,
                "GEPA_EXECUTION_ID": record.execution_id,
                "GEPA_PARENT_SHA": record.requested_parent_sha,
                "GEPA_WORKTREE": lease.worktree_path,
                "GEPA_ARTIFACTS": lease.artifact_path,
            },
            inherit_host_env=True,
        )


class ApptainerRuntimeBackend:
    name = "apptainer"

    def __init__(self, run_dir: Path, config: dict[str, Any]):
        self.run_dir = run_dir
        self.config = config
        self.executor_config = dict(config.get("executor") or {})
        self.apptainer_config = dict(self.executor_config.get("apptainer") or {})
        self.container_repo = str(self.apptainer_config.get("container_repo", "/workspace/repo"))
        self.container_artifacts = str(self.apptainer_config.get("container_artifacts", "/workspace/artifacts"))
        self.container_scratch = str(self.apptainer_config.get("container_scratch", "/workspace/scratch"))
        self.container_home = str(self.apptainer_config.get("container_home", "/workspace/home"))

    def prepare(
        self,
        candidate: Candidate,
        lease: WorkspaceLease,
        record: ExecutionRecord,
    ) -> RuntimeLease:
        image = self._required_text("image")
        if not Path(image).expanduser().exists():
            raise RuntimeBackendError(f"executor.apptainer.image does not exist: {image}")
        apptainer = str(self.apptainer_config.get("executable", "apptainer"))
        if shutil.which(apptainer) is None and not Path(apptainer).expanduser().exists():
            raise RuntimeBackendError(f"apptainer executable was not found: {apptainer}")

        host_artifacts = Path(lease.artifact_path).expanduser().resolve()
        host_repo = Path(lease.worktree_path).expanduser().resolve()

        # Per-execution directory structure using execution_id
        execution_id = record.execution_id
        host_scratch = host_artifacts / f"scratch_{execution_id}"
        host_home = host_artifacts / f"home_{execution_id}"
        host_tmp = host_scratch / "tmp"

        for path in (host_artifacts, host_scratch, host_home, host_tmp):
            path.mkdir(parents=True, exist_ok=True)
        self._copy_home_template(host_home)

        prefix = [apptainer, "exec"]
        if bool(self.apptainer_config.get("cleanenv", True)):
            prefix.append("--cleanenv")
        if bool(self.apptainer_config.get("containall", True)):
            prefix.append("--containall")
        if bool(self.apptainer_config.get("writable_tmpfs", True)):
            prefix.append("--writable-tmpfs")
        if bool(self.apptainer_config.get("userns", False)):
            prefix.append("--userns")
        prefix.extend(str(a) for a in self.apptainer_config.get("extra_exec_args", []) or [])
        # Update container paths for per-execution structure
        container_scratch = f"{self.container_artifacts}/scratch_{execution_id}"
        container_home = f"{self.container_artifacts}/home_{execution_id}"

        # --home both bind-mounts host_home->container_home AND sets HOME=container_home
        # inside the container. (Passing HOME via --env is silently ignored by apptainer.)
        prefix.extend(["--home", f"{host_home}:{container_home}"])
        prefix.extend(["--pwd", self.container_repo])
        for bind in self._binds(host_repo, host_artifacts, host_scratch):
            prefix.extend(["--bind", bind])

        # Build environment variables for --env passing
        env = self._allowed_host_env()
        env.update(
            {
                "GEPA_CANDIDATE_ID": candidate.candidate_id,
                "GEPA_EXECUTION_ID": record.execution_id,
                "GEPA_PARENT_SHA": record.requested_parent_sha,
                "GEPA_WORKTREE": self.container_repo,
                "GEPA_ARTIFACTS": self.container_artifacts,
                "TMPDIR": f"{container_scratch}/tmp",
            }
        )

        # Add environment variables via --env mechanism (Apptainer best practice)
        for key, value in env.items():
            # Properly escape the value for shell command line
            escaped_value = self._escape_env_value(value)
            prefix.extend(["--env", f"{key}={escaped_value}"])

        prefix.append(image)
        return RuntimeLease(
            backend=self.name,
            repo_path=self.container_repo,
            artifact_path=self.container_artifacts,
            host_cwd=str(host_repo),
            command=str(self.apptainer_config.get("command", self.config.get("agent", {}).get("command", "claude"))),
            command_prefix=prefix,
            env={},  # Empty since environment variables passed via --env
            inherit_host_env=False,
            artifacts={
                "host_repo": str(host_repo),
                "host_artifacts": str(host_artifacts),
                "host_scratch": str(host_scratch),
                "host_home": str(host_home),
            },
        )

    def _required_text(self, field_name: str) -> str:
        value = self.apptainer_config.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise RuntimeBackendError(f"executor.apptainer.{field_name}: required non-empty string")
        return str(Path(value).expanduser())

    def _copy_home_template(self, host_home: Path) -> None:
        template = self.apptainer_config.get("claude_home_template")
        if not template:
            return
        source = Path(str(template)).expanduser().resolve()
        if not source.exists():
            raise RuntimeBackendError(f"executor.apptainer.claude_home_template does not exist: {source}")
        if source.is_file():
            raise RuntimeBackendError("executor.apptainer.claude_home_template must be a directory")
        shutil.copytree(source, host_home, dirs_exist_ok=True)

    def _binds(self, host_repo: Path, host_artifacts: Path, host_scratch: Path) -> list[str]:
        # NOTE: host_home->container_home is intentionally NOT bound here; it is
        # established by the ``--home`` flag in prepare() (which also sets HOME).
        binds = [
            f"{host_repo}:{self.container_repo}",
            f"{host_artifacts}:{self.container_artifacts}",
            f"{host_scratch}:{self.container_scratch}",
        ]
        for item in self.config.get("workspace", {}).get("readonly_assets", []):
            source = Path(str(item["source"])).expanduser().resolve()
            target = self.container_repo.rstrip("/") + "/" + str(item["target"]).lstrip("/")
            binds.append(f"{source}:{target}:ro")
        binds.extend(self._configured_binds("readonly_binds", readonly=True))
        binds.extend(self._configured_binds("extra_binds", readonly=False))
        return binds

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
        names = ["PATH"]
        names.extend(str(item) for item in self.apptainer_config.get("env_allowlist", []))
        return {name: os.environ[name] for name in dict.fromkeys(names) if name in os.environ}

    def _escape_env_value(self, value: str) -> str:
        """Escape environment variable value for shell command line arguments.

        This method properly handles quotes, spaces, and special characters
        to ensure safe passing of environment values via Apptainer's --env.
        """
        if not value:
            return ""

        # If the value contains only safe characters, no escaping needed
        safe_chars = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-/:=@")
        if all(c in safe_chars for c in value):
            return value

        # Otherwise, wrap in single quotes and escape embedded single quotes
        # Single quote escaping: 'value_with_'`'"'embedded'quotes''
        escaped = value.replace("'", "'\"'\"'")
        return f"'{escaped}'"


def runtime_backend_for(config: dict[str, Any], run_dir: Path):
    backend = str(config.get("executor", {}).get("runtime_backend", "local"))
    if backend == "local":
        return LocalRuntimeBackend(run_dir, config)
    if backend == "apptainer":
        return ApptainerRuntimeBackend(run_dir, config)
    raise RuntimeBackendError(f"executor.runtime_backend: unknown backend {backend!r}")
