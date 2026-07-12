"""Config-driven Apptainer executor image materialization.

Replaces the old interactive ``setup_apptainer.py`` wizard. Instead of asking the
user to run a separate, docker-dependent, file-extension-heuristic setup step, this
module derives what the executor container must provide *from the resolved GEPA
config*, builds (or reuses) a thin SIF via dockerless ``apptainer build ... docker://``,
caches it by a content fingerprint, and merges the derived image path / binds /
``--userns`` flag back into the resolved config.

Design notes
------------
- The image is intentionally THIN. For CVMFS-backed projects (e.g. JUNO/OMILREC)
  the toolchain (gcc/cmake/ROOT/python) comes from a read-only ``/cvmfs`` bind, so
  the image only needs ``bash``/coreutils plus a glibc that matches the CVMFS-built
  binaries (hence ``almalinux:9`` when CVMFS is detected).
- Claude Code is NOT baked in. The host nvm node-version directory is bound
  read-only at the same absolute path inside the container, so the (already
  allowlisted) host ``PATH`` resolves ``claude`` with zero rewriting.
- Pure-Python (non-CVMFS) projects get ``docker://python:3.11-slim`` instead.
- Building never requires Docker: ``apptainer build out.sif docker://<base>`` pulls
  OCI layers directly from the registry.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..agents.agent_client import resolve_command


CACHE_DIR = Path(os.environ.get("GEPA_IMAGE_CACHE_DIR", "~/.cache/gepa/images")).expanduser()
_DEFAULT_TINY = "docker://alpine:3.20"
_HOST_PROBE_TTL_SECONDS = 30 * 24 * 3600  # 30 days


class MaterializationError(RuntimeError):
    """Raised when the executor image cannot be built or validated."""


# --------------------------------------------------------------------------- #
# Dataclasses
# --------------------------------------------------------------------------- #


@dataclass
class Requirements:
    tools: list[str]
    cvmfs_required: bool
    cvmfs_paths: list[str]
    is_pure_python: bool
    suggested_base: str
    image_required_tools: list[str]  # subset the IMAGE itself must provide
    claude_command: str


@dataclass
class ClaudeBind:
    enabled: bool
    nvm_node_dir: str | None = None
    claude_bin: str | None = None
    node_bin: str | None = None
    container_path_prefix: str | None = None  # <nvm_node_dir>/bin, for container PATH


@dataclass
class ImageMaterialization:
    sif_path: str
    fingerprint: str
    base_image: str
    requirements: Requirements
    claude_bind: ClaudeBind
    userns: bool
    derived_readonly_binds: list[dict] = field(default_factory=list)
    derived_env_allowlist: list[str] = field(default_factory=list)
    diagnostics: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Pure, offline-testable derivation
# --------------------------------------------------------------------------- #

# Canonical tool name -> regex used to detect it in command strings.
_TOOL_PATTERNS: dict[str, str] = {
    "python3": r"\bpython3\b",
    "python": r"\bpython\b",
    "git": r"\bgit\b",
    "gcc": r"\bgcc\b",
    "g++": r"\bg\+\+\b",
    "cmake": r"\bcmake\b",
    "make": r"\bmake\b",
    "pytest": r"\bpytest\b",
    "bash": r"\bbash\b",
    "root": r"\broot\b",
}
_BUILD_TOOLS = {"gcc", "g++", "cmake", "make"}
_PURE_PYTHON_OK = {"python3", "python", "pytest", "bash", "git"}


def _collect_command_strings(resolved_config: dict) -> list[str]:
    """All command strings that the executor may run, from the resolved config."""
    runtime = resolved_config.get("runtime") or {}
    commands: list[str] = list(runtime.get("allowed_commands") or [])
    python_cmd = runtime.get("python_command")
    if python_cmd:
        commands.append(str(python_cmd))
    # Defense in depth: also pull setup + benchmark + validation from contracts/task.
    contracts = resolved_config.get("contracts") or {}
    commands.extend((contracts.get("runtime") or {}).get("setup_commands") or [])
    commands.extend((resolved_config.get("task") or {}).get("benchmark_commands") or [])
    commands.extend((resolved_config.get("task") or {}).get("validation_commands") or [])
    return [str(item) for item in commands if item]


def derive_requirements(resolved_config: dict) -> Requirements:
    """Derive image requirements from the resolved config (pure)."""
    blob = "\n".join(_collect_command_strings(resolved_config))
    tools: list[str] = []
    for name, pattern in _TOOL_PATTERNS.items():
        if re.search(pattern, blob):
            tools.append(name)
    # Drop the bare 'python' token if 'python3' is present (avoid double counting).
    if "python3" in tools and "python" in tools:
        tools.remove("python")

    cvmfs_required = "/cvmfs/" in blob
    cvmfs_paths = ["/cvmfs"] if cvmfs_required else []

    has_build_tools = bool(_BUILD_TOOLS & set(tools))
    is_pure_python = (
        not cvmfs_required
        and not has_build_tools
        and set(tools).issubset(_PURE_PYTHON_OK)
    )

    if cvmfs_required or has_build_tools:
        suggested_base = "docker://almalinux:9"
    else:
        suggested_base = "docker://python:3.11-slim"

    # What the IMAGE itself must provide. CVMFS supplies the compiler/python stack;
    # pure-python images must ship python3. bash is always required.
    image_required_tools = ["bash"]
    if is_pure_python:
        image_required_tools.append("python3")

    agent_command = str((resolved_config.get("agent") or {}).get("command") or "claude")
    return Requirements(
        tools=tools,
        cvmfs_required=cvmfs_required,
        cvmfs_paths=cvmfs_paths,
        is_pure_python=is_pure_python,
        suggested_base=suggested_base,
        image_required_tools=image_required_tools,
        claude_command=agent_command,
    )


_NODE_VERSION_RE = re.compile(r"^v\d+\.\d+\.\d+$")


def _find_nvm_node_dir(path: Path) -> Path | None:
    """Walk up from ``path`` to the nvm ``versions/node/vX.Y.Z`` directory."""
    current = path
    for _ in range(20):
        if _NODE_VERSION_RE.match(current.name) and current.parent.name == "node":
            return current
        if current.parent == current:
            break
        current = current.parent
    return None


def resolve_claude_bind(agent_command: str) -> ClaudeBind:
    """Resolve how to expose the host claude+node inside the container.

    Uses :func:`agent_client.resolve_command` (filesystem only, no apptainer subprocess).
    Binds the entire nvm node-version directory at the same absolute path so the host
    ``PATH`` entry ``<nvm_node_dir>/bin`` resolves ``claude``/``node`` unchanged.
    """
    resolution = resolve_command(agent_command)
    if resolution is None:
        return ClaudeBind(enabled=False)

    argv = resolution.argv
    claude_bin: Path | None = None
    if len(argv) == 1:
        claude_bin = Path(argv[0])
    elif len(argv) >= 2:
        # [node, cli-wrapper.cjs] form: derive claude bin from the nvm dir.
        wrapper = Path(argv[1])
        nvm_dir = _find_nvm_node_dir(wrapper)
        if nvm_dir is not None:
            candidate = nvm_dir / "bin" / "claude"
            claude_bin = candidate if candidate.exists() else None

    if claude_bin is None or not claude_bin.exists():
        return ClaudeBind(enabled=False)

    nvm_node_dir = _find_nvm_node_dir(claude_bin)
    if nvm_node_dir is None:
        return ClaudeBind(enabled=False)
    node_bin = nvm_node_dir / "bin" / "node"
    if not node_bin.exists():
        return ClaudeBind(enabled=False)

    return ClaudeBind(
        enabled=True,
        nvm_node_dir=str(nvm_node_dir),
        claude_bin=str(claude_bin),
        node_bin=str(node_bin),
        container_path_prefix=str(nvm_node_dir / "bin"),
    )


def _fingerprint(
    base: str,
    tools: list[str],
    claude_bind: ClaudeBind,
    extra_packages: list[str],
) -> str:
    """Stable content hash; invalidates when claude is upgraded or moved."""
    claude_bin = claude_bind.claude_bin
    claude_mtime: float | None = None
    if claude_bin:
        try:
            claude_mtime = float(Path(claude_bin).stat().st_mtime)
        except OSError:
            claude_mtime = None
    payload = {
        "base": base,
        "tools": sorted(set(tools)),
        "nvm_node_dir": claude_bind.nvm_node_dir,
        "claude_bin": claude_bin,
        "claude_bin_mtime": claude_mtime,
        "extra_packages": sorted(extra_packages),
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Subprocess side-effects
# --------------------------------------------------------------------------- #


def _run(argv: list[str], *, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )


def _stderr_tail(text: str, limit: int = 1200) -> str:
    text = (text or "").strip()
    return text[-limit:] if len(text) > limit else text


def _apptainer_version(apptainer: str) -> str:
    try:
        result = _run([apptainer, "--version"], timeout=15)
        if result.returncode == 0:
            return (result.stdout or result.stderr).strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return ""


def _probe_host_runtime(apptainer: str, *, tiny: str = _DEFAULT_TINY) -> dict:
    """Detect whether default exec works or ``--userns`` is required (cached)."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / "host_probe.json"
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if time.time() - float(cached.get("probed_at", 0)) < _HOST_PROBE_TTL_SECONDS:
                return cached
        except (ValueError, OSError):
            pass

    version = _apptainer_version(apptainer)
    default = _run([apptainer, "exec", tiny, "true"], timeout=120)
    default_ok = default.returncode == 0
    userns = _run([apptainer, "exec", "--userns", tiny, "true"], timeout=120)
    userns_ok = userns.returncode == 0

    result = {
        "apptainer": apptainer,
        "version": version,
        "default_exec_ok": default_ok,
        "userns_exec_ok": userns_ok,
        # Prefer setuid (default) when it works; only force userns when needed.
        "userns": (not default_ok) and userns_ok,
        "default_stderr_tail": _stderr_tail(default.stderr),
        "userns_stderr_tail": _stderr_tail(userns.stderr),
        "probed_at": time.time(),
    }
    try:
        cache_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    except OSError:
        pass
    return result


def _build_sif(base: str, out: Path, *, timeout: int = 1200) -> None:
    """Build a SIF from an OCI base without Docker. Atomic rename on success."""
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp_out = out.with_suffix(out.suffix + f".tmp.{os.getpid()}")
    if tmp_out.exists():
        tmp_out.unlink()
    result = _run(["apptainer", "build", str(tmp_out), base], timeout=timeout)
    if result.returncode != 0 or not tmp_out.exists():
        try:
            tmp_out.unlink()
        except OSError:
            pass
        raise MaterializationError(
            "Failed to build Apptainer image.\n"
            f"  Command: apptainer build {tmp_out} {base}\n"
            f"  stderr:\n{_stderr_tail(result.stderr)}\n"
            "Mitigation: pre-build the image yourself (`apptainer build out.sif "
            f"{base}`), point execution.apptainer.image at it, and set "
            "execution.apptainer.auto_image: false."
        )
    os.replace(tmp_out, out)


def _probe_tools(
    sif: Path,
    req: Requirements,
    claude_bind: ClaudeBind,
    *,
    userns: bool,
    apptainer: str,
) -> dict:
    """Validate the built image can actually run the required tools + claude."""
    diagnostics: dict[str, Any] = {"tools": {}, "claude": None, "warnings": []}
    base_argv = [apptainer, "exec"]
    if userns:
        base_argv.append("--userns")
    if claude_bind.enabled and claude_bind.nvm_node_dir:
        base_argv += ["--bind", f"{claude_bind.nvm_node_dir}:{claude_bind.nvm_node_dir}:ro"]

    # Strict: tools the image itself must provide.
    strict_tools = list(req.image_required_tools)
    # git is desired but not required (the orchestrator owns git on the host).
    desired_extra = ["git"] if "git" not in strict_tools else []

    for tool in strict_tools + desired_extra:
        probe_argv = base_argv + [str(sif), tool, "--version"]
        result = _run(probe_argv, timeout=60)
        entry = {
            "ok": result.returncode == 0,
            "version": (result.stdout or result.stderr).strip().splitlines()[0]
            if (result.returncode == 0 and (result.stdout or result.stderr).strip())
            else "",
        }
        diagnostics["tools"][tool] = entry
        if tool in desired_extra and not entry["ok"]:
            diagnostics["warnings"].append(
                f"'{tool}' not found in image (optional; bind or change base_image if needed)"
            )

    # Claude: verify the bound binary + node are executable inside the container.
    if claude_bind.enabled and claude_bind.claude_bin and claude_bind.node_bin:
        check = (
            f"test -x {claude_bind.claude_bin} && test -x {claude_bind.node_bin} "
            f"&& echo claude-ok"
        )
        probe_argv = base_argv + [str(sif), "sh", "-c", check]
        result = _run(probe_argv, timeout=60)
        diagnostics["claude"] = {
            "ok": result.returncode == 0 and "claude-ok" in (result.stdout or ""),
            "claude_bin": claude_bind.claude_bin,
        }
    else:
        diagnostics["warnings"].append(
            "host claude/node could not be resolved; executor will need claude inside the image"
        )
        diagnostics["claude"] = {"ok": False, "claude_bin": None}

    # Missing a STRICT tool is fatal.
    missing_strict = [
        tool for tool in strict_tools if not diagnostics["tools"].get(tool, {}).get("ok")
    ]
    if missing_strict:
        raise MaterializationError(
            "Built image is missing required tool(s): "
            f"{', '.join(missing_strict)}. Use execution.apptainer.base_image to pick a "
            "base that includes them."
        )
    if not diagnostics["claude"]["ok"]:
        raise MaterializationError(
            "Claude Code is not reachable inside the container via the host nvm bind "
            f"({claude_bind.claude_bin}). Install claude in the image or fix the bind."
        )
    return diagnostics


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def _lock_path_for(sif: Path) -> Path:
    return sif.with_suffix(sif.suffix + ".lock")


def _wait_for_sif(sif: Path, *, max_seconds: int = 1800) -> bool:
    deadline = time.monotonic() + max_seconds
    while time.monotonic() < deadline:
        if sif.exists():
            return True
        time.sleep(2.0)
    return sif.exists()


def materialize_executor_image(
    resolved_config: dict,
    *,
    force: bool = False,
    host_probe: dict | None = None,
) -> ImageMaterialization:
    """Build (or reuse) the executor SIF and validate it. Returns the materialization."""
    apptainer_cfg = (resolved_config.get("executor") or {}).get("apptainer") or {}
    apptainer_exe = str(apptainer_cfg.get("executable") or "apptainer")

    req = derive_requirements(resolved_config)
    claude_bind = resolve_claude_bind(req.claude_command)
    base_image = str(apptainer_cfg.get("base_image") or req.suggested_base)
    extra_packages = list(apptainer_cfg.get("extra_packages") or [])

    fingerprint = _fingerprint(base_image, req.tools, claude_bind, extra_packages)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    sif_path = CACHE_DIR / f"{fingerprint}.sif"

    if force and sif_path.exists():
        try:
            sif_path.unlink()
        except OSError:
            pass

    probe = host_probe if host_probe is not None else _probe_host_runtime(apptainer_exe)
    if not probe.get("default_exec_ok") and not probe.get("userns_exec_ok"):
        raise MaterializationError(
            "Apptainer cannot run containers on this host.\n"
            f"  default exec stderr:\n{_stderr_tail(probe.get('default_stderr_tail', ''))}\n"
            f"  --userns exec stderr:\n{_stderr_tail(probe.get('userns_stderr_tail', ''))}\n"
            "Install/fix apptainer (setuid or unprivileged user namespaces)."
        )
    userns = bool(probe.get("userns"))

    built_now = False
    build_ms: int | None = None
    if not sif_path.exists():
        # Concurrent-run safe: another process may be building the same fingerprint.
        lock_path = _lock_path_for(sif_path)
        import fcntl  # local import; fcntl is POSIX-only

        with open(lock_path, "w") as lock_fh:
            try:
                fcntl.flock(lock_fh, fcntl.LOCK_EX)
            except OSError:
                pass  # flock unavailable; proceed single-process
            if not sif_path.exists():
                if _wait_for_sif(sif_path, max_seconds=5):
                    pass  # appeared just now
                else:
                    start = time.monotonic()
                    _build_sif(base_image, sif_path)
                    build_ms = int((time.monotonic() - start) * 1000)
                    built_now = True

    tool_diagnostics = _probe_tools(
        sif_path, req, claude_bind, userns=userns, apptainer=apptainer_exe
    )

    derived_binds: list[dict] = []
    if req.cvmfs_required and Path("/cvmfs").is_dir():
        derived_binds.append({"source": "/cvmfs", "target": "/cvmfs", "mode": "ro"})
    elif req.cvmfs_required:
        tool_diagnostics["warnings"].append(
            "/cvmfs referenced by commands but not mounted on host; CVMFS bind skipped"
        )
    if claude_bind.enabled and claude_bind.nvm_node_dir:
        derived_binds.append(
            {"source": claude_bind.nvm_node_dir, "target": claude_bind.nvm_node_dir, "mode": "ro"}
        )

    diagnostics = {
        **tool_diagnostics,
        "base_image": base_image,
        "suggested_base": req.suggested_base,
        "fingerprint": fingerprint,
        "cache_path": str(sif_path),
        "cache_hit": not built_now,
        "build_ms": build_ms,
        "host_probe": {
            "version": probe.get("version"),
            "default_exec_ok": probe.get("default_exec_ok"),
            "userns_exec_ok": probe.get("userns_exec_ok"),
            "userns": userns,
        },
    }
    return ImageMaterialization(
        sif_path=str(sif_path),
        fingerprint=fingerprint,
        base_image=base_image,
        requirements=req,
        claude_bind=claude_bind,
        userns=userns,
        derived_readonly_binds=derived_binds,
        derived_env_allowlist=[],
        diagnostics=diagnostics,
    )


def _bind_key(item: Any) -> tuple:
    if isinstance(item, dict):
        return (str(item.get("source")), str(item.get("target")))
    return (str(item),)


def _merge_binds(existing: list, derived: list[dict]) -> list:
    """Append derived binds, deduping by (source, target). Preserves user order."""
    result = list(existing)
    seen = {_bind_key(item) for item in result}
    for item in derived:
        key = _bind_key(item)
        if key not in seen:
            result.append(item)
            seen.add(key)
    return result


def _sanitize_materialization(mat: ImageMaterialization) -> dict:
    data = asdict(mat)
    return data


def _image_present(apptainer_cfg: dict) -> bool:
    image = apptainer_cfg.get("image")
    if not image:
        return False
    return Path(str(image)).expanduser().exists()


def finalize_runtime(
    resolved_config: dict,
    *,
    allow_materialize: bool = True,
    force: bool = False,
) -> dict:
    """Entry point called from the CLI after load_and_resolve.

    Probes the host (cached), auto-sets ``--userns`` when required, and (when
    materialization is enabled and the image is missing) builds/validates the SIF and
    merges the derived image path + binds into the resolved config. Idempotent.
    """
    executor = resolved_config.setdefault("executor", {})
    if executor.get("runtime_backend") != "apptainer":
        return resolved_config

    apptainer = executor.setdefault("apptainer", {})
    apptainer_exe = str(apptainer.get("executable") or "apptainer")
    host_probe = _probe_host_runtime(apptainer_exe)

    # Always honor a userns requirement when the user did not pin it explicitly,
    # so execution actually works on hosts whose apptainer lacks setuid.
    if "userns" not in apptainer and host_probe.get("userns"):
        apptainer["userns"] = True

    auto_image = apptainer.get("auto_image", None)  # None == auto
    present = _image_present(apptainer)
    do_materialize = (
        allow_materialize
        and (auto_image is not False)
        and (force or not present)
    )

    meta = resolved_config.setdefault("_meta", {})
    if not do_materialize:
        meta["_materialization"] = {
            "materialized": False,
            "image_present": present,
            "auto_image": auto_image,
            "host_probe": {
                "version": host_probe.get("version"),
                "userns": host_probe.get("userns"),
            },
        }
        return resolved_config

    mat = materialize_executor_image(resolved_config, force=force, host_probe=host_probe)
    apptainer["image"] = mat.sif_path
    apptainer["readonly_binds"] = _merge_binds(
        apptainer.get("readonly_binds") or [], mat.derived_readonly_binds
    )
    meta["_materialization"] = _sanitize_materialization(mat)
    return resolved_config


# --------------------------------------------------------------------------- #
# Convenience for the CLI subcommand / tests
# --------------------------------------------------------------------------- #


def last_materialization(resolved_config: dict) -> dict | None:
    return (resolved_config.get("_meta") or {}).get("_materialization")


# Kept importable for tests that want to swap the subprocess runner.
def _set_run_runner(_: Callable[..., subprocess.CompletedProcess]) -> None:  # pragma: no cover
    global _run
    _run = _
