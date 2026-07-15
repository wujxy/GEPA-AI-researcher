from __future__ import annotations

import fnmatch
import json
import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """User-facing configuration error with a precise field path."""


TASK_TOP_KEYS = {
    "kind", "task", "project", "metric", "validation", "safety", "loop", "selection",
    "executor", "judger", "usage_tracking", "evidence",
}
PROFILE_TOP_KEYS = {
    "kind", "name", "source", "docs", "provided_paths", "reference", "isolation",
    "repo_overlays", "skills", "agent", "safety", "pre_materialized_lfs_paths",
    "generated_tracked_paths", "hash_artifacts",
}


def load_config_file(path: Path) -> dict[str, Any]:
    path = path.expanduser().resolve()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"config: cannot read {path}: {exc}") from exc
    try:
        if path.suffix.lower() == ".json":
            data = json.loads(text)
        elif path.suffix.lower() in {".yaml", ".yml"}:
            data = yaml.safe_load(text)
        else:
            raise ConfigError(f"config: unsupported extension {path.suffix!r}; use .json, .yaml, or .yml")
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise ConfigError(f"config: invalid syntax in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("config: top-level value must be an object")
    return data


def load_and_resolve(
    config_path: Path,
    *,
    run_dir: Path | None = None,
    resume: bool = False,
) -> dict[str, Any]:
    config_path = config_path.expanduser().resolve()
    raw = load_config_file(config_path)
    if raw.get("kind") != "task":
        raise ConfigError("kind: run configuration must have kind: task")
    resolved = _resolve_task(raw, config_path)
    if resume and run_dir is None:
        raise ConfigError("resume: --resume requires an explicit --run-dir")
    if run_dir is not None:
        resolved["run_dir"] = str(run_dir.expanduser().resolve())
    resolved["resume"] = bool(resume)
    return resolved


def _resolve_task(raw_task: dict[str, Any], task_path: Path) -> dict[str, Any]:
    task_config = _canonical_task(raw_task)
    _validate_task(task_config)
    project = task_config["project"]
    source_files = {"task": str(task_path)}
    if "profile" in project:
        profile_path = _path(project["profile"], task_path.parent)
        profile = _canonical_profile(load_config_file(profile_path))
        profile_base = profile_path.parent
        source_files["profile"] = str(profile_path)
    else:
        profile = _canonical_profile({"kind": "project_profile", "name": "inline", **deepcopy(project["inline"])})
        profile_base = task_path.parent
        source_files["profile"] = "inline"
    _validate_profile(profile)

    source = profile.get("source") or {}
    repo_path = _optional_path(source.get("path"), profile_base)
    requested_ref = str(project.get("ref") or source.get("default_ref") or "")
    workspace_mode = str(source.get("workspace_mode") or ("git_worktree" if repo_path else "artifact_directory"))
    if workspace_mode == "git_worktree" and not repo_path:
        raise ConfigError("project: git_worktree requires profile.source.path")
    if repo_path and not repo_path.exists():
        raise ConfigError(f"source.path: path does not exist: {repo_path}")
    if workspace_mode == "git_worktree" and not requested_ref:
        raise ConfigError("project.ref: git_worktree projects require a ref or profile default_ref")
    resolved_sha = _resolve_git_ref(repo_path, requested_ref) if repo_path and requested_ref else ""

    docs = _resolve_docs(profile.get("docs") or [], profile_base)
    provided_paths = _resolve_provided_paths(profile.get("provided_paths") or [], profile_base)
    repo_overlays = _resolve_repo_overlays(profile.get("repo_overlays") or [], profile_base)
    reference = deepcopy(profile.get("reference") or {})
    reference_commands = _dedupe(list(reference.get("commands") or []))
    reference_note = str(reference.get("note") or "User-provided references only; GEPA must not auto-execute them.")
    safety = _merge_safety(profile.get("safety") or {}, task_config.get("safety") or {})

    loop = task_config["loop"]
    selection = task_config.get("selection") or {}
    executor_task = task_config.get("executor") or {}
    metric = deepcopy(task_config["metric"])
    validation = deepcopy(task_config.get("validation") or {"checks": []})
    max_rounds = int(loop.get("max_rounds", 1))
    candidates = int(loop.get("candidates_per_round", 3))
    seed_count = int(loop.get("seed_count", candidates))
    min_rounds = int(loop.get("min_rounds", min(max_rounds, 2)))
    patience = int(loop.get("patience", 2))
    worker_cap = int(loop.get("max_parallel_candidates", candidates))
    timeout = int(executor_task.get("timeout_seconds", (profile.get("agent") or {}).get("timeout_seconds", 600)))

    isolation = profile.get("isolation") or {}
    backend = str(isolation.get("backend") or "local")
    workdir = str(isolation.get("workdir") or "/workspace/repo")
    agent_profile = profile.get("agent") or {}
    command = str(agent_profile.get("command") or isolation.get("command") or "claude")
    image = str(isolation.get("image") or "")
    if image:
        image = str(_path(image, profile_base)) if _looks_like_path(image) else image

    mounts = _dedupe_mounts(
        _compile_repo_overlays(repo_overlays, workdir)
        + _provided_path_mounts(provided_paths)
        + _doc_mounts(docs, provided_paths)
    )
    apptainer = _default_apptainer_options(_resolve_apptainer(dict(isolation.get("apptainer") or {}), profile_base))
    runtime_spec = {
        "backend": backend,
        "image": image,
        "workdir": workdir,
        "command": command,
        "env": _canonical_env(profile),
        "setup": [],
        "check": [],
        "mounts": mounts,
        "tools": [],
        "apptainer": apptainer,
    }
    runtime_contract = {
        "backend": backend,
        "image": image,
        "workdir": workdir,
        "command": command,
        "setup": [],
        "check": [],
        "tools": [],
        "guarantee": "User guarantees the provided paths are sufficient to run the project; GEPA only binds and reports them.",
    }

    task_section = {
        **deepcopy(task_config["task"]),
        "data_files": [item["path"] for item in provided_paths if item.get("role") in {"data", "data_file", "input_data", "resource_pack"}],
        "benchmark_commands": [metric["command"]] if metric.get("command") else [],
        "validation_commands": [check["command"] for check in validation.get("checks", []) if check.get("command")],
        "artifacts": [],
        "samples": list((task_config.get("task") or {}).get("samples") or []) or [{"sample_id": "task_execution"}],
    }
    if repo_path and workspace_mode == "git_worktree":
        task_section["repo_paths"] = [str(repo_path)]

    resources_contract = {
        "repo_path": str(repo_path) if repo_path else None,
        "docs": docs,
        "context_paths": docs,
        "provided_paths": provided_paths,
        "accessible_paths": [item["path"] for item in provided_paths if item.get("mode") == "ro"],
        "writable_paths": [item["path"] for item in provided_paths if item.get("mode") == "rw"],
        "repo_overlays": repo_overlays,
        "skills": list(profile.get("skills") or []),
    }
    resolved: dict[str, Any] = {
        "_meta": {
            "schema_version": "canonical",
            "source_files": source_files,
            "warnings": [],
            "resolution": {"task": "canonical task config", "profile": profile.get("name", "inline"), "defaults": "gepa_researcher.config"},
        },
        "_runtime_spec": runtime_spec,
        "components": {"mode": "claude_code_agents"},
        "agent": {"command": command, "cwd": str(repo_path or task_path.parent), "timeout_seconds": timeout, "extra_args": list(agent_profile.get("extra_args") or []), "model": agent_profile.get("model"), "env": dict(agent_profile.get("env") or {})},
        "runtime": runtime_contract,
        "task": task_section,
        "context": {"paths": docs, "notes": [reference_note], "skills": list(profile.get("skills") or [])},
        "budget": {"max_rounds": max_rounds, "min_rounds": min_rounds, "no_improvement_patience": patience},
        "generation": {"batch_size": candidates, "enable_merge": bool(loop.get("enable_merge", False))},
        "gepa": {
            "frontier_policy": str(selection.get("frontier_policy", "pareto")),
            "acceptance_policy": str(selection.get("acceptance_policy", "minibatch_improves_then_pareto")),
            "minibatch_size": int(selection.get("minibatch_size", 1)),
            "parent_sampling": str(selection.get("parent_sampling", "pareto_win_weighted")),
            "feedback_sample_ids": list(selection.get("feedback_sample_ids") or []),
            "pareto_sample_ids": list(selection.get("pareto_sample_ids") or []),
        },
        "executor": {"max_workers": worker_cap, "executor_timeout_seconds": timeout, "repair_retries": int(executor_task.get("repair_retries", 1)), "fail_fast": False, "runtime_backend": backend},
        "judger": {
            "pass_threshold": float((task_config.get("judger") or {}).get("pass_threshold", 0.85)),
            "repair_retries": int((task_config.get("judger") or {}).get("repair_retries", 1)),
            "repair_timeout_seconds": int((task_config.get("judger") or {}).get("repair_timeout_seconds", 300)),
        },
        "initialization": {"seed_count": seed_count},
        "evidence": _resolve_evidence(task_config.get("evidence") or {}),
        "usage_tracking": _resolve_usage_tracking(task_config.get("usage_tracking") or {}),
        "contracts": {
            "objective": deepcopy(task_config["task"]),
            "metric": metric,
            "validation": validation,
            "resources": resources_contract,
            "reference": {"commands": reference_commands, "note": reference_note},
            "safety": safety,
            "runtime": runtime_contract,
        },
    }
    if repo_path:
        resolved["workspace"] = {
            "mode": workspace_mode,
            "repo_path": str(repo_path),
            "baseline_ref": resolved_sha or requested_ref,
            "requested_ref": requested_ref,
            "resolved_sha": resolved_sha,
            "pre_materialized_lfs_paths": list(profile.get("pre_materialized_lfs_paths") or []),
            "generated_tracked_paths": list(profile.get("generated_tracked_paths") or []),
            "hash_artifacts": list(profile.get("hash_artifacts") or []),
        }
    if safety:
        resolved["candidate_policy"] = {
            "allowed_target_globs": list(safety.get("editable_paths") or []),
            "frozen_globs": list(safety.get("frozen_paths") or []),
            "max_target_files": int(safety.get("max_files_per_candidate", 1_000_000)),
        }
    return resolved


def _canonical_task(raw: dict[str, Any]) -> dict[str, Any]:
    _reject_unknown(raw, TASK_TOP_KEYS, "")
    if isinstance(raw.get("metric"), dict):
        _reject_unknown(raw["metric"], {"name", "direction", "command", "description", "unit", "repeats", "improvement"}, "metric")
    data = deepcopy(raw)
    return {
        "kind": "task",
        "task": deepcopy(data.get("task") or {}),
        "project": deepcopy(data.get("project") or {}),
        "metric": deepcopy(data.get("metric") or {}),
        "validation": deepcopy(data.get("validation") or {"checks": []}),
        "safety": deepcopy(data.get("safety") or {}),
        "loop": deepcopy(data.get("loop") or {}),
        "selection": deepcopy(data.get("selection") or {}),
        "executor": deepcopy(data.get("executor") or {}),
        "judger": deepcopy(data.get("judger") or {}),
        "usage_tracking": deepcopy(data.get("usage_tracking") or {}),
        "evidence": deepcopy(data.get("evidence") or {}),
    }


def _canonical_profile(raw: dict[str, Any]) -> dict[str, Any]:
    _reject_unknown(raw, PROFILE_TOP_KEYS, "")
    data = deepcopy(raw)
    reference = deepcopy(data.get("reference") or {})
    reference.setdefault("note", "User-provided references only; GEPA must not auto-execute them.")
    isolation = deepcopy(data.get("isolation") or {})
    isolation.setdefault("backend", "local")
    isolation.setdefault("mode", "bind_paths")
    return {
        "kind": "project_profile",
        "name": str(data.get("name") or "project"),
        "source": deepcopy(data.get("source") or {}),
        "docs": list(data.get("docs") or []),
        "provided_paths": list(data.get("provided_paths") or []),
        "reference": reference,
        "isolation": isolation,
        "repo_overlays": list(data.get("repo_overlays") or []),
        "skills": list(data.get("skills") or []),
        "agent": deepcopy(data.get("agent") or {}),
        "safety": deepcopy(data.get("safety") or {}),
        "pre_materialized_lfs_paths": list(data.get("pre_materialized_lfs_paths") or []),
        "generated_tracked_paths": list(data.get("generated_tracked_paths") or []),
        "hash_artifacts": list(data.get("hash_artifacts") or []),
    }


def _validate_task(data: dict[str, Any]) -> None:
    if data.get("kind") != "task":
        raise ConfigError("kind: run configuration must have kind: task")
    task = _section(data.get("task"), "task")
    _required_text(task, "name", "task.name")
    _required_text(task, "goal", "task.goal")
    project = _section(data.get("project"), "project")
    if ("profile" in project) == ("inline" in project):
        raise ConfigError("project: provide exactly one of profile or inline")
    metric = _section(data.get("metric"), "metric")
    if not metric.get("name"):
        metric["name"] = "primary"
    _required_text(metric, "direction", "metric.direction")
    if metric["direction"] not in {"minimize", "maximize"}:
        raise ConfigError("metric.direction: expected 'minimize' or 'maximize'")
    if not metric.get("command") and not metric.get("description"):
        raise ConfigError("metric: provide at least one of command or description")
    loop = _section(data.get("loop"), "loop")
    if "max_rounds" not in loop:
        raise ConfigError("loop.max_rounds: expected integer >= 0")
    for key in ("max_rounds", "min_rounds", "patience", "candidates_per_round", "max_parallel_candidates", "seed_count"):
        if key in loop:
            _positive_int(loop[key], f"loop.{key}", allow_zero=(key in {"max_rounds", "min_rounds"}))
    _validate_safety(_section(data.get("safety") or {}, "safety"), "safety")


def _validate_profile(profile: dict[str, Any]) -> None:
    if profile.get("kind") != "project_profile":
        raise ConfigError("profile.kind: expected 'project_profile'")
    _required_text(profile, "name", "profile.name")
    source = _section(profile.get("source") or {}, "source")
    mode = source.get("workspace_mode", "git_worktree" if source.get("path") else "artifact_directory")
    if mode not in {"git_worktree", "artifact_directory"}:
        raise ConfigError("source.workspace_mode: expected 'git_worktree' or 'artifact_directory'")
    _string_list(profile.get("docs") or [], "docs")
    for index, item in enumerate(profile.get("provided_paths") or []):
        if not isinstance(item, dict):
            raise ConfigError(f"provided_paths[{index}]: expected object")
        _required_text(item, "path", f"provided_paths[{index}].path")
        if item.get("mode", "ro") not in {"ro", "rw"}:
            raise ConfigError(f"provided_paths[{index}].mode: expected 'ro' or 'rw'")
    reference = _section(profile.get("reference") or {}, "reference")
    _string_list(reference.get("commands") or [], "reference.commands")
    isolation = _section(profile.get("isolation") or {}, "isolation")
    if isolation.get("backend", "local") not in {"local", "apptainer"}:
        raise ConfigError("isolation.backend: expected 'local' or 'apptainer'")
    _validate_safety(_section(profile.get("safety") or {}, "safety"), "safety")


def _resolve_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "visualize_when_applicable": bool(evidence.get("visualize_when_applicable", False)),
        "plot_selection_policy": str(evidence.get("plot_selection_policy", "proposer_selects")),
        "artifact_formats": list(evidence.get("artifact_formats") or []),
        "guidance": str(evidence.get("guidance", "")),
    }


def _resolve_usage_tracking(usage: dict[str, Any]) -> dict[str, Any]:
    return {
        "enabled": bool(usage.get("enabled", True)),
        "persist_raw_envelope": bool(usage.get("persist_raw_envelope", True)),
        "print_round_summary": bool(usage.get("print_round_summary", True)),
        "print_run_summary": bool(usage.get("print_run_summary", True)),
    }


def _resolve_docs(docs: list[str], base: Path) -> list[str]:
    return [str(_path(item, base)) for item in docs]


def _resolve_provided_paths(items: list[Any], base: Path) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(items):
        if isinstance(item, str):
            item = {"path": item, "mode": "ro", "role": "provided_path"}
        if not isinstance(item, dict):
            raise ConfigError(f"provided_paths[{index}]: expected object")
        path = str(_path(item["path"], base))
        mode = str(item.get("mode") or "ro")
        role = str(item.get("role") or "provided_path")
        note = str(item.get("note") or item.get("description") or "")
        key = (path, mode)
        if key in seen:
            continue
        seen.add(key)
        result.append({"path": path, "mode": mode, "role": role, "note": note})
    return result


def _resolve_repo_overlays(overlays: list[dict[str, Any]], base: Path) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for index, item in enumerate(overlays):
        source = _path(item["source"], base)
        if not source.exists():
            raise ConfigError(f"repo_overlays[{index}].source: source does not exist: {source} (resolved relative to profile_dir)")
        result.append({"source": str(source), "target": str(item["target"]), "mode": str(item.get("mode") or "ro"), "purpose": str(item.get("purpose") or "")})
    return result


def _compile_repo_overlays(overlays: list[dict[str, str]], workdir: str) -> list[dict[str, str]]:
    return [{"source": item["source"], "target": f"{workdir}/{str(item['target']).lstrip('/')}", "mode": str(item.get("mode") or "ro")} for item in overlays]


def _provided_path_mounts(paths: list[dict[str, str]]) -> list[dict[str, str]]:
    return [{"source": item["path"], "target": item["path"], "mode": item.get("mode", "ro")} for item in paths]


def _doc_mounts(docs: list[str], provided_paths: list[dict[str, str]]) -> list[dict[str, str]]:
    provided = [Path(item["path"]) for item in provided_paths]
    mounts: list[dict[str, str]] = []
    for doc in docs:
        path = Path(doc)
        if not any(path == root or root in path.parents for root in provided):
            mounts.append({"source": str(path), "target": str(path), "mode": "ro"})
    return mounts


def _resolve_apptainer(apptainer: dict[str, Any], base: Path) -> dict[str, Any]:
    result = deepcopy(apptainer)
    for field in ("image", "claude_home_template", "claude_host_home"):
        if result.get(field):
            result[field] = str(_path(result[field], base))
    for field in ("readonly_binds", "extra_binds", "home_readonly_binds"):
        values = []
        for item in result.get(field, []) or []:
            values.append({**item, "source": str(_path(item["source"], base))} if isinstance(item, dict) else item)
        if values:
            result[field] = values
    result.setdefault("executable", "apptainer")
    return result


def _default_apptainer_options(apptainer: dict[str, Any]) -> dict[str, Any]:
    result = {
        "executable": "apptainer",
        "cleanenv": False,
        "containall": False,
        "writable_tmpfs": True,
        "userns": True,
        "auto_init_claude_home": True,
        "extra_exec_args": [],
        "home_readonly_binds": [],
    }
    result.update(apptainer)
    return result


def _canonical_env(profile: dict[str, Any]) -> dict[str, Any]:
    env = dict((profile.get("isolation") or {}).get("env") or {})
    return {"pass": _dedupe(list(env.get("pass") or [])), "set": dict(env.get("set") or {})}


def _merge_safety(profile: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    profile_editable = list(profile.get("editable_paths") or [])
    task_editable = list(task.get("editable_paths") or [])
    if task_editable and profile_editable:
        invalid = [pattern for pattern in task_editable if not _within(pattern, profile_editable)]
        if invalid:
            raise ConfigError(f"safety.editable_paths: task paths broaden profile policy: {invalid}")
        editable = task_editable
    else:
        editable = task_editable or profile_editable
    frozen = list(dict.fromkeys(list(profile.get("frozen_paths") or []) + list(task.get("frozen_paths") or [])))
    result: dict[str, Any] = {"editable_paths": editable, "frozen_paths": frozen}
    if any(section.get("max_files_per_candidate") is not None for section in (profile, task)):
        result["max_files_per_candidate"] = min(
            int(section["max_files_per_candidate"])
            for section in (profile, task)
            if section.get("max_files_per_candidate") is not None
        )
    return {key: value for key, value in result.items() if value not in (None, [], {})}


def _within(pattern: str, ceilings: list[str]) -> bool:
    if pattern in ceilings:
        return True
    if not any(character in pattern for character in "*?["):
        return any(fnmatch.fnmatch(pattern, ceiling) for ceiling in ceilings)
    return False


def _validate_safety(safety: dict[str, Any], path: str) -> None:
    allowed = {"editable_paths", "frozen_paths", "max_files_per_candidate"}
    unknown = sorted(set(safety) - allowed)
    if unknown:
        raise ConfigError(f"{path}.{unknown[0]}: unknown safety key")
    _string_list(safety.get("editable_paths", []), f"{path}.editable_paths")
    _string_list(safety.get("frozen_paths", []), f"{path}.frozen_paths")
    if "max_files_per_candidate" in safety:
        _positive_int(safety["max_files_per_candidate"], f"{path}.max_files_per_candidate")


def _resolve_git_ref(repo: Path, ref: str) -> str:
    if not repo.exists():
        raise ConfigError(f"source.path: path does not exist: {repo}")
    completed = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", f"{ref}^{{commit}}"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if completed.returncode != 0:
        raise ConfigError(f"project.ref: cannot resolve {ref!r} in {repo}: {completed.stderr.strip()}")
    return completed.stdout.strip()


def explain_config(config: dict[str, Any]) -> str:
    meta = config.get("_meta") or {}
    lines = ["Resolved GEPA configuration", ""]
    lines.extend(f"- source.{label}: {source}" for label, source in (meta.get("source_files") or {}).items())
    lines.extend([
        f"- task.name: {config.get('task', {}).get('name')} (task config)",
        f"- budget.max_rounds: {config.get('budget', {}).get('max_rounds')} (task/default)",
        f"- generation.batch_size: {config.get('generation', {}).get('batch_size')} (task/default)",
        f"- executor.max_workers: {config.get('executor', {}).get('max_workers')} (profile/default cap)",
        f"- workspace.repo_path: {config.get('workspace', {}).get('repo_path')} (profile)",
        f"- workspace.resolved_sha: {config.get('workspace', {}).get('resolved_sha')} (resolved)",
    ])
    warnings = list(meta.get("warnings") or [])
    if warnings:
        lines.append("- warnings:")
        lines.extend(f"  - {warning}" for warning in warnings)
    return "\n".join(lines)


def sanitize_snapshot(value: Any) -> Any:
    sensitive = ("token", "secret", "password", "credential", "api-key", "api_key")
    if isinstance(value, dict):
        return {str(key): "<redacted>" if any(part in str(key).lower() for part in sensitive) else sanitize_snapshot(item) for key, item in value.items()}
    if isinstance(value, list):
        result = []
        redact_next = False
        for item in value:
            if redact_next:
                result.append("<redacted>")
                redact_next = False
                continue
            if isinstance(item, str) and any(part in item.lower() for part in sensitive):
                lowered = item.lower()
                if "=" in item:
                    key = item.split("=", 1)[0]
                    result.append(f"{key}=<redacted>")
                else:
                    result.append(item)
                    redact_next = lowered.startswith("-")
                continue
            result.append(sanitize_snapshot(item))
        return result
    return value


def _reject_unknown(data: dict[str, Any], allowed: set[str], path: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        prefix = f"{path}." if path else ""
        raise ConfigError(f"{prefix}{unknown[0]}: unknown field")


def _section(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{name}: expected object")
    return value


def _required_text(data: dict[str, Any], key: str, path: str) -> None:
    if not isinstance(data.get(key), str) or not data[key].strip():
        raise ConfigError(f"{path}: required non-empty string")


def _positive_int(value: Any, path: str, allow_zero: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < (0 if allow_zero else 1):
        operator = ">= 0" if allow_zero else ">= 1"
        raise ConfigError(f"{path}: expected integer {operator}")


def _string_list(value: Any, path: str) -> None:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConfigError(f"{path}: expected list of strings")


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(str(item) for item in items if str(item)))


def _dedupe_mounts(mounts: list[dict[str, str]]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in mounts:
        key = (str(item["source"]), str(item["target"]), str(item.get("mode") or "ro"))
        if key in seen:
            continue
        seen.add(key)
        result.append({"source": key[0], "target": key[1], "mode": key[2]})
    return result


def _looks_like_path(value: str) -> bool:
    return value.startswith("/") or value.startswith("./") or value.startswith("../") or value.endswith(".sif")


def _path(value: Any, base: Path) -> Path:
    path = Path(str(value)).expanduser()
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def _optional_path(value: Any, base: Path) -> Path | None:
    return _path(value, base) if value else None
