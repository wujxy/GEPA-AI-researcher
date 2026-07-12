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


TASK_KEYS = {
    "schema_version", "kind", "task", "project", "metric", "validation",
    "safety", "budget", "initialization", "generation", "gepa", "judger",
    "usage_tracking", "evidence",
}
PROFILE_KEYS = {
    "schema_version", "kind", "name", "source", "runtime", "resources", "agent", "safety",
    # Kept for the legacy v1 resolver path; schema v2 profiles reject these by using
    # V2_PROFILE_KEYS below.
    "environment", "execution",
}
V2_PROFILE_KEYS = {"schema_version", "kind", "name", "source", "runtime", "resources", "agent", "safety"}

SECTION_KEYS = {
    "task": {"name", "goal", "samples"},
    "project": {"profile", "ref", "inline"},
    "metric": {"name", "direction", "command", "description", "unit", "repeats", "improvement"},
    "validation": {"checks"},
    "improvement": {"mode", "minimum"},
    "check": {"name", "command", "success_criteria"},
    "safety": {"editable_paths", "frozen_paths", "max_files_per_candidate", "max_commits_per_candidate"},
    "budget": {"max_rounds", "min_rounds", "patience", "candidates_per_round"},
    "initialization": {"seed_count"},
    "generation": {"batch_size", "enable_merge"},
    "gepa": {
        "minibatch_size", "feedback_sample_ids", "pareto_sample_ids",
        "frontier_policy", "acceptance_policy", "parent_sampling",
    },
    "judger": {"pass_threshold"},
    "usage_tracking": {"enabled", "persist_raw_envelope", "print_round_summary", "print_run_summary"},
    "evidence": {"visualize_when_applicable", "plot_selection_policy", "artifact_formats", "guidance"},
    "source": {"repo_path", "default_ref", "workspace_mode"},
    "resources": {"data_files", "context_paths", "skills", "readonly_assets", "pre_materialized_lfs_paths", "generated_tracked_paths", "hash_artifacts"},
    "agent": {"command", "timeout_seconds", "extra_args"},
    "environment": {"description", "setup_commands", "python_command", "dependency_policy"},
    "execution": {"runtime_backend", "lifecycle", "max_parallel_candidates", "fail_fast", "apptainer"},
    "apptainer": {"image", "executable", "command", "container_repo", "container_artifacts", "container_scratch", "container_home", "claude_home_template", "claude_host_home", "base_image", "container_claude_dir", "install_command", "auto_image", "cleanenv", "containall", "writable_tmpfs", "userns", "auto_bind_claude_auth", "auto_init_claude_home", "env_allowlist", "extra_exec_args", "extra_packages", "source_scripts", "passthrough_environment", "validation_commands", "runtime_init", "readonly_binds", "extra_binds", "home_readonly_binds"},
    "runtime_init": {"description", "setup_commands", "python_command", "dependency_policy", "validation_commands"},
    "asset": {"source", "target"},
    "bind": {"source", "target", "mode"},
    "runtime": {"backend", "workdir", "command", "append_agent_args", "apptainer", "env", "setup", "check", "mounts"},
    "runtime_apptainer": {"image", "executable", "auto_image", "base_image", "extra_packages", "cleanenv", "containall", "writable_tmpfs", "userns", "extra_exec_args", "claude_home_template", "claude_host_home", "auto_init_claude_home", "home_readonly_binds", "install_command", "container_claude_dir", "auto_bind_claude_auth"},
    "runtime_env": {"pass", "set"},
    "runtime_mounts": {"repo", "extra"},
    "runtime_extra_mount": {"source", "target", "mode"},
}
LEGACY_UNUSED_FIELDS = (
    "gepa.frontier_policy",
    "gepa.acceptance_policy",
    "gepa.parent_sampling",
    "candidate_policy.allow_merge",
    "workspace.retention",
    "workspace.keep_accepted",
    "executor.per_candidate_workspace",
)


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
    if raw.get("schema_version") == 2:
        if raw.get("kind") != "task":
            raise ConfigError("kind: run configuration must have kind: task")
        resolved = _resolve_task_v2(raw, config_path)
    elif raw.get("schema_version") == 1 or raw.get("kind") in {"task", "project_profile"}:
        if raw.get("kind") != "task":
            raise ConfigError("kind: run configuration must have kind: task")
        resolved = _resolve_task(raw, config_path)
    else:
        resolved = _resolve_legacy(raw, config_path)
    if resume and run_dir is None and resolved.get("_meta", {}).get("schema_version") != 0:
        raise ConfigError("resume: --resume requires an explicit --run-dir")
    if run_dir is not None:
        resolved["run_dir"] = str(run_dir.expanduser().resolve())
    if resolved.get("_meta", {}).get("schema_version") != 0 or resume:
        resolved["resume"] = bool(resume)
    return resolved


def _resolve_task_v2(task_config: dict[str, Any], task_path: Path) -> dict[str, Any]:
    _validate_task_v2(task_config)
    project = task_config["project"]
    source_files = {"task": str(task_path)}
    if "profile" in project:
        profile_path = _path(project["profile"], task_path.parent)
        profile = load_config_file(profile_path)
        _validate_profile_v2(profile)
        profile_base = profile_path.parent
        source_files["profile"] = str(profile_path)
    else:
        profile = {
            "schema_version": 2,
            "kind": "project_profile",
            "name": "inline",
            **deepcopy(project["inline"]),
        }
        _validate_profile_v2(profile)
        profile_base = task_path.parent
        source_files["profile"] = "inline"

    source = deepcopy(profile.get("source") or {})
    repo_path = _optional_path(source.get("repo_path"), profile_base)
    requested_ref = str(project.get("ref") or source.get("default_ref") or "")
    workspace_mode = str(source.get("workspace_mode") or ("git_worktree" if repo_path else "artifact_directory"))
    if workspace_mode == "git_worktree" and not repo_path:
        raise ConfigError("project: git_worktree requires profile.source.repo_path")
    if repo_path and not repo_path.exists():
        raise ConfigError(f"source.repo_path: path does not exist: {repo_path}")
    if workspace_mode == "git_worktree" and not requested_ref:
        raise ConfigError("project.ref: git_worktree projects require a ref or profile default_ref")
    resolved_sha = _resolve_git_ref(repo_path, requested_ref) if repo_path and requested_ref else ""

    safety = _merge_safety(profile.get("safety") or {}, task_config.get("safety") or {})
    resources = _resolve_resources_v2(profile.get("resources") or {}, profile_base)
    runtime_ir = _compile_runtime_ir(profile["runtime"], profile_base)
    metric = deepcopy(task_config["metric"])
    validation = deepcopy(task_config.get("validation") or {"checks": []})
    budget = task_config["budget"]
    generation_config = deepcopy(task_config.get("generation") or {})
    initialization_config = deepcopy(task_config.get("initialization") or {})
    gepa_config = deepcopy(task_config.get("gepa") or {})
    judger_config = deepcopy(task_config.get("judger") or {})
    usage_tracking_config = deepcopy(task_config.get("usage_tracking") or {})
    evidence_config = deepcopy(task_config.get("evidence") or {})
    candidates = int(generation_config.get("batch_size", budget.get("candidates_per_round", 3)))
    seed_count = int(initialization_config.get("seed_count", candidates))
    max_rounds = int(budget["max_rounds"])
    patience = int(budget.get("patience", 2))
    min_rounds = int(budget.get("min_rounds", min(max_rounds, 2)))
    timeout = int((profile.get("agent") or {}).get("timeout_seconds", 600))
    validation_commands = [check["command"] for check in validation.get("checks", []) if check.get("command")]
    benchmark_commands = [metric["command"]] if metric.get("command") else []
    task_section = {
        **deepcopy(task_config["task"]),
        "data_files": resources["data_files"],
        "benchmark_commands": benchmark_commands,
        "validation_commands": validation_commands,
        "artifacts": [],
        "samples": list(task_config["task"].get("samples") or []) or [{"sample_id": "task_execution"}],
    }
    if repo_path and workspace_mode == "git_worktree":
        task_section["repo_paths"] = [str(repo_path)]

    runtime_contract = {
        "backend": runtime_ir["backend"],
        "workdir": runtime_ir["workdir"],
        "command": runtime_ir["command"],
        "init": runtime_ir["init"],
        "preflight": runtime_ir["preflight"],
    }
    resolved = {
        "_meta": {
            "schema_version": 2,
            "source_files": source_files,
            "warnings": [],
            "resolution": {"task": "task config", "profile": profile.get("name", "inline"), "defaults": "gepa_researcher.config"},
        },
        "_runtime_ir": runtime_ir,
        "components": {"mode": "claude_code_agents"},
        "agent": {"command": runtime_ir["command"], "cwd": str(repo_path or task_path.parent), "timeout_seconds": timeout, "extra_args": list((profile.get("agent") or {}).get("extra_args") or [])},
        "runtime": runtime_contract,
        "task": task_section,
        "context": {"paths": resources["context_paths"], "notes": [], "skills": resources["skills"]},
        "budget": {"max_rounds": max_rounds, "min_rounds": min_rounds, "no_improvement_patience": patience},
        "generation": {"batch_size": candidates, "enable_merge": bool(generation_config.get("enable_merge", False))},
        "gepa": {
            "frontier_policy": str(gepa_config.get("frontier_policy", "pareto")),
            "acceptance_policy": str(gepa_config.get("acceptance_policy", "minibatch_improves_then_pareto")),
            "minibatch_size": int(gepa_config.get("minibatch_size", 1)),
            "parent_sampling": str(gepa_config.get("parent_sampling", "pareto_win_weighted")),
            "feedback_sample_ids": list(gepa_config.get("feedback_sample_ids") or []),
            "pareto_sample_ids": list(gepa_config.get("pareto_sample_ids") or []),
        },
        "executor": {"max_workers": min(candidates, 3), "executor_timeout_seconds": timeout, "fail_fast": False, "runtime_backend": runtime_ir["backend"]},
        "judger": {"pass_threshold": float(judger_config.get("pass_threshold", 0.85))},
        "initialization": {"seed_count": seed_count},
        "evidence": {
            "visualize_when_applicable": bool(evidence_config.get("visualize_when_applicable", False)),
            "plot_selection_policy": str(evidence_config.get("plot_selection_policy", "proposer_selects")),
            "artifact_formats": list(evidence_config.get("artifact_formats") or []),
            "guidance": str(evidence_config.get("guidance", "")),
        },
        "execution": {"lifecycle": "materialize_once" if workspace_mode == "git_worktree" else "stateless"},
        "usage_tracking": {
            "enabled": bool(usage_tracking_config.get("enabled", True)),
            "persist_raw_envelope": bool(usage_tracking_config.get("persist_raw_envelope", True)),
            "print_round_summary": bool(usage_tracking_config.get("print_round_summary", True)),
            "print_run_summary": bool(usage_tracking_config.get("print_run_summary", True)),
        },
        "contracts": {"objective": deepcopy(task_config["task"]), "metric": metric, "validation": validation, "resources": {"data_files": resources["data_files"], "repo_path": str(repo_path) if repo_path else None, "context_paths": resources["context_paths"], "skills": resources["skills"]}, "safety": safety, "runtime": runtime_contract},
    }
    # Keep this mirror until container image materialization is moved to _runtime_ir.
    if runtime_ir["backend"] == "apptainer":
        resolved["executor"]["apptainer"] = runtime_ir["apptainer"]
    if repo_path:
        resolved["workspace"] = {"mode": workspace_mode, "repo_path": str(repo_path), "baseline_ref": resolved_sha or requested_ref, "requested_ref": requested_ref, "resolved_sha": resolved_sha, "pre_materialized_lfs_paths": resources["pre_materialized_lfs_paths"], "generated_tracked_paths": resources["generated_tracked_paths"], "hash_artifacts": resources["hash_artifacts"]}
    if safety:
        resolved["candidate_policy"] = {"allowed_target_globs": list(safety.get("editable_paths") or []), "frozen_globs": list(safety.get("frozen_paths") or []), "max_target_files": int(safety.get("max_files_per_candidate", 1_000_000)), "max_commits": int(safety.get("max_commits_per_candidate", 1))}
    return resolved


def _resolve_task(task_config: dict[str, Any], task_path: Path) -> dict[str, Any]:
    _validate_task(task_config)
    project = task_config["project"]
    source_files = {"task": str(task_path)}
    if "profile" in project:
        profile_path = _path(project["profile"], task_path.parent)
        profile = load_config_file(profile_path)
        _validate_profile(profile)
        profile_base = profile_path.parent
        source_files["profile"] = str(profile_path)
    else:
        profile = {
            "schema_version": 1,
            "kind": "project_profile",
            "name": "inline",
            **deepcopy(project["inline"]),
        }
        _validate_profile(profile)
        profile_base = task_path.parent
        source_files["profile"] = "inline"

    source = deepcopy(profile.get("source") or {})
    repo_path = _optional_path(source.get("repo_path"), profile_base)
    requested_ref = str(project.get("ref") or source.get("default_ref") or "")
    workspace_mode = str(source.get("workspace_mode") or ("git_worktree" if repo_path else "artifact_directory"))
    if workspace_mode == "git_worktree" and not repo_path:
        raise ConfigError("project: git_worktree requires profile.source.repo_path")
    if repo_path and not repo_path.exists():
        raise ConfigError(f"source.repo_path: path does not exist: {repo_path}")
    if workspace_mode == "git_worktree" and not requested_ref:
        raise ConfigError("project.ref: git_worktree projects require a ref or profile default_ref")
    resolved_sha = _resolve_git_ref(repo_path, requested_ref) if repo_path and requested_ref else ""

    safety = _merge_safety(profile.get("safety") or {}, task_config.get("safety") or {})
    resources = _resolve_resources(profile.get("resources") or {}, profile_base)
    environment = deepcopy(profile.get("environment") or {})
    metric = deepcopy(task_config["metric"])
    validation = deepcopy(task_config.get("validation") or {"checks": []})
    budget = task_config["budget"]
    generation_config = deepcopy(task_config.get("generation") or {})
    initialization_config = deepcopy(task_config.get("initialization") or {})
    gepa_config = deepcopy(task_config.get("gepa") or {})
    judger_config = deepcopy(task_config.get("judger") or {})
    usage_tracking_config = deepcopy(task_config.get("usage_tracking") or {})
    evidence_config = deepcopy(task_config.get("evidence") or {})
    candidates = int(generation_config.get("batch_size", budget.get("candidates_per_round", 3)))
    seed_count = int(initialization_config.get("seed_count", candidates))
    max_rounds = int(budget["max_rounds"])
    patience = int(budget.get("patience", 2))
    min_rounds = int(budget.get("min_rounds", min(max_rounds, 2)))
    execution_profile = deepcopy(profile.get("execution") or {})
    worker_cap = int(execution_profile.get("max_parallel_candidates", 3))
    runtime_backend = str(execution_profile.get("runtime_backend", "local"))
    apptainer_config = _resolve_apptainer(execution_profile.get("apptainer") or {}, profile_base)
    agent_profile = deepcopy(profile.get("agent") or {})

    benchmark_commands = [metric["command"]] if metric.get("command") else []
    validation_commands = [
        check["command"] for check in validation.get("checks", []) if check.get("command")
    ]
    configured_samples = list(task_config["task"].get("samples") or [])
    task_section: dict[str, Any] = {
        **deepcopy(task_config["task"]),
        "data_files": resources["data_files"],
        "benchmark_commands": benchmark_commands,
        "validation_commands": validation_commands,
        "artifacts": [],
        "samples": configured_samples or [{"sample_id": "task_execution"}],
    }
    if repo_path and workspace_mode == "git_worktree":
        task_section["repo_paths"] = [str(repo_path)]

    runtime_contract = {
        "description": environment.get("description", ""),
        "setup_commands": list(environment.get("setup_commands") or []),
        "python_command": environment.get("python_command", ""),
        "dependency_policy": environment.get("dependency_policy", ""),
    }
    if runtime_backend == "apptainer":
        apptainer_config = _attach_apptainer_runtime_init(apptainer_config, runtime_contract)
    contracts = {
        "objective": deepcopy(task_config["task"]),
        "metric": metric,
        "validation": validation,
        "resources": {
            "data_files": resources["data_files"],
            "repo_path": str(repo_path) if repo_path else None,
            "context_paths": resources["context_paths"],
            "skills": resources["skills"],
        },
        "safety": safety,
        "runtime": runtime_contract,
    }

    timeout = int(agent_profile.get("timeout_seconds", 600))
    resolved: dict[str, Any] = {
        "_meta": {
            "schema_version": 1,
            "source_files": source_files,
            "warnings": [],
            "resolution": {
                "task": "task config",
                "profile": profile.get("name", "inline"),
                "defaults": "gepa_researcher.config",
            },
        },
        "components": {"mode": "claude_code_agents"},
        "agent": {
            "command": str(agent_profile.get("command", "claude")),
            "cwd": str(repo_path or task_path.parent),
            "timeout_seconds": timeout,
            "extra_args": list(agent_profile.get("extra_args") or []),
        },
        "runtime": {
            "environment": environment.get("description", ""),
            "python_command": environment.get("python_command", ""),
            "dependency_policy": environment.get("dependency_policy", ""),
            "allowed_commands": list(dict.fromkeys(
                list(environment.get("setup_commands") or []) + benchmark_commands + validation_commands
            )),
        },
        "task": task_section,
        "context": {
            "paths": resources["context_paths"],
            "notes": [],
            "skills": resources["skills"],
        },
        "budget": {
            "max_rounds": max_rounds,
            "min_rounds": min_rounds,
            "no_improvement_patience": patience,
        },
        "generation": {
            "batch_size": candidates,
            "enable_merge": bool(generation_config.get("enable_merge", False)),
        },
        "gepa": {
            "frontier_policy": str(gepa_config.get("frontier_policy", "pareto")),
            "acceptance_policy": str(gepa_config.get("acceptance_policy", "minibatch_improves_then_pareto")),
            "minibatch_size": int(gepa_config.get("minibatch_size", 1)),
            "parent_sampling": str(gepa_config.get("parent_sampling", "pareto_win_weighted")),
            "feedback_sample_ids": list(gepa_config.get("feedback_sample_ids") or []),
            "pareto_sample_ids": list(gepa_config.get("pareto_sample_ids") or []),
        },
        "executor": {
            "max_workers": min(candidates, worker_cap),
            "executor_timeout_seconds": timeout,
            "fail_fast": bool(execution_profile.get("fail_fast", False)),
            "runtime_backend": runtime_backend,
            **({"apptainer": apptainer_config} if runtime_backend == "apptainer" else {}),
        },
        "judger": {"pass_threshold": float(judger_config.get("pass_threshold", 0.85))},
        "initialization": {"seed_count": seed_count},
        "evidence": {
            "visualize_when_applicable": bool(evidence_config.get("visualize_when_applicable", False)),
            "plot_selection_policy": str(evidence_config.get("plot_selection_policy", "proposer_selects")),
            "artifact_formats": list(evidence_config.get("artifact_formats") or []),
            "guidance": str(evidence_config.get("guidance", "")),
        },
        "execution": {
            "lifecycle": str(execution_profile.get("lifecycle") or (
                "materialize_once" if workspace_mode == "git_worktree" else "stateless"
            ))
        },
        "usage_tracking": {
            "enabled": bool(usage_tracking_config.get("enabled", True)),
            "persist_raw_envelope": bool(usage_tracking_config.get("persist_raw_envelope", True)),
            "print_round_summary": bool(usage_tracking_config.get("print_round_summary", True)),
            "print_run_summary": bool(usage_tracking_config.get("print_run_summary", True)),
        },
        "contracts": contracts,
    }
    if repo_path:
        resolved["workspace"] = {
            "mode": workspace_mode,
            "repo_path": str(repo_path),
            "baseline_ref": resolved_sha or requested_ref,
            "requested_ref": requested_ref,
            "resolved_sha": resolved_sha,
            "readonly_assets": resources["readonly_assets"],
            "pre_materialized_lfs_paths": resources["pre_materialized_lfs_paths"],
            "generated_tracked_paths": resources["generated_tracked_paths"],
            "hash_artifacts": resources["hash_artifacts"],
        }
    if safety:
        resolved["candidate_policy"] = {
            "allowed_target_globs": list(safety.get("editable_paths") or []),
            "frozen_globs": list(safety.get("frozen_paths") or []),
            "max_target_files": int(safety.get("max_files_per_candidate", 1_000_000)),
            "max_commits": int(safety.get("max_commits_per_candidate", 1)),
        }
    return resolved


def _resolve_legacy(raw: dict[str, Any], config_path: Path) -> dict[str, Any]:
    resolved = deepcopy(raw)
    warnings = ["legacy config detected; migrate to schema_version: 1 task/profile configuration"]
    warnings.extend(
        f"unused legacy field: {field}"
        for field in LEGACY_UNUSED_FIELDS
        if _has_dotted(raw, field)
    )
    resolved["_meta"] = {
        "schema_version": 0,
        "source_files": {"task": str(config_path)},
        "warnings": warnings,
        "resolution": {"legacy": "preserved"},
    }
    resolved.setdefault("contracts", _legacy_contracts(resolved))
    return resolved


def _legacy_contracts(config: dict[str, Any]) -> dict[str, Any]:
    task = config.get("task") or {}
    policy = config.get("candidate_policy") or {}
    runtime = config.get("runtime") or {}
    validation_commands = list(task.get("validation_commands") or [])
    metric_command = next(iter(task.get("benchmark_commands") or []), None)
    return {
        "objective": {"name": task.get("name", ""), "goal": task.get("goal", "")},
        "metric": {
            "name": "primary",
            "direction": "minimize" if "minimiz" in str(task.get("goal", "")).lower() else "maximize",
            "command": metric_command,
            "description": task.get("goal", ""),
        },
        "validation": {
            "checks": [
                {
                    "name": f"legacy_check_{index + 1}",
                    "command": command,
                    "success_criteria": "command succeeds",
                }
                for index, command in enumerate(validation_commands)
            ]
        },
        "resources": {
            "data_files": list(task.get("data_files") or []),
            "repo_path": next(iter(task.get("repo_paths") or []), None),
            "context_paths": list((config.get("context") or {}).get("paths") or []),
            "skills": list((config.get("context") or {}).get("skills") or []),
        },
        "safety": {
            "editable_paths": list(policy.get("allowed_target_globs") or []),
            "frozen_paths": list(policy.get("frozen_globs") or []),
            "max_files_per_candidate": policy.get("max_target_files"),
            "max_commits_per_candidate": policy.get("max_commits"),
        },
        "runtime": {
            "description": runtime.get("environment", ""),
            "setup_commands": [],
            "python_command": runtime.get("python_command", ""),
            "dependency_policy": runtime.get("dependency_policy", ""),
        },
    }


def _validate_task_v2(data: dict[str, Any]) -> None:
    _unknown(data, TASK_KEYS, "")
    _equal(data.get("schema_version"), 2, "schema_version")
    _equal(data.get("kind"), "task", "kind")
    task = _section(data.get("task"), "task")
    _required_text(task, "name", "task.name")
    _required_text(task, "goal", "task.goal")
    samples = task.get("samples", [])
    if samples is not None:
        if not isinstance(samples, list):
            raise ConfigError("task.samples: expected list")
        for index, sample in enumerate(samples):
            if not isinstance(sample, dict):
                raise ConfigError(f"task.samples[{index}]: expected object")
            sample_id = sample.get("sample_id")
            if not isinstance(sample_id, str) or not sample_id.strip():
                raise ConfigError(f"task.samples[{index}].sample_id: required non-empty string")
    project = _section(data.get("project"), "project")
    if ("profile" in project) == ("inline" in project):
        raise ConfigError("project: provide exactly one of profile or inline")
    if "profile" in project:
        _required_text(project, "profile", "project.profile")
    if "ref" in project:
        _required_text(project, "ref", "project.ref")
    if "inline" in project and not isinstance(project["inline"], dict):
        raise ConfigError("project.inline: expected object")
    _validate_metric_validation_budget_common(data)


def _validate_metric_validation_budget_common(data: dict[str, Any]) -> None:
    metric = _section(data.get("metric"), "metric")
    _optional_text_fields(metric, ("command", "description", "unit"), "metric")
    _required_text(metric, "name", "metric.name")
    if metric.get("direction") not in {"minimize", "maximize"}:
        raise ConfigError("metric.direction: expected 'minimize' or 'maximize'")
    if not metric.get("command") and not metric.get("description"):
        raise ConfigError("metric: provide at least one of command or description")
    if "repeats" in metric:
        _positive_int(metric["repeats"], "metric.repeats")
    if "improvement" in metric:
        improvement = _section(metric["improvement"], "improvement", "metric.improvement")
        if improvement.get("mode") not in {"absolute", "relative_percent"}:
            raise ConfigError("metric.improvement.mode: expected 'absolute' or 'relative_percent'")
        minimum = improvement.get("minimum")
        if isinstance(minimum, bool) or not isinstance(minimum, (int, float)):
            raise ConfigError("metric.improvement.minimum: expected number")
    validation = _section(data.get("validation") or {"checks": []}, "validation")
    checks = validation.get("checks", [])
    if not isinstance(checks, list):
        raise ConfigError("validation.checks: expected list")
    for index, check in enumerate(checks):
        path = f"validation.checks[{index}]"
        check = _section(check, "check", path)
        _required_text(check, "name", f"{path}.name")
        _required_text(check, "success_criteria", f"{path}.success_criteria")
        if "command" in check:
            _required_text(check, "command", f"{path}.command")
    _validate_safety(_section(data.get("safety") or {}, "safety"), "safety")
    budget = _section(data.get("budget"), "budget")
    _positive_int(budget.get("max_rounds"), "budget.max_rounds", allow_zero=True)
    if "min_rounds" in budget:
        _positive_int(budget["min_rounds"], "budget.min_rounds", allow_zero=True)
    if "patience" in budget:
        _positive_int(budget["patience"], "budget.patience")
    if "candidates_per_round" in budget:
        _positive_int(budget["candidates_per_round"], "budget.candidates_per_round")
    initialization = _section(data.get("initialization") or {}, "initialization")
    if "seed_count" in initialization:
        _positive_int(initialization["seed_count"], "initialization.seed_count")
    generation = _section(data.get("generation") or {}, "generation")
    if "batch_size" in generation:
        _positive_int(generation["batch_size"], "generation.batch_size")
    if "enable_merge" in generation and not isinstance(generation["enable_merge"], bool):
        raise ConfigError("generation.enable_merge: expected boolean")
    gepa = _section(data.get("gepa") or {}, "gepa")
    if "minibatch_size" in gepa:
        _positive_int(gepa["minibatch_size"], "gepa.minibatch_size")
    for field in ("feedback_sample_ids", "pareto_sample_ids"):
        _string_list(gepa.get(field, []), f"gepa.{field}")
    _optional_text_fields(gepa, ("frontier_policy", "acceptance_policy", "parent_sampling"), "gepa")
    judger = _section(data.get("judger") or {}, "judger")
    if "pass_threshold" in judger:
        value = judger["pass_threshold"]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError("judger.pass_threshold: expected number")
    usage_tracking = _section(data.get("usage_tracking") or {}, "usage_tracking")
    for field in ("enabled", "persist_raw_envelope", "print_round_summary", "print_run_summary"):
        if field in usage_tracking and not isinstance(usage_tracking[field], bool):
            raise ConfigError(f"usage_tracking.{field}: expected boolean")
    evidence = _section(data.get("evidence") or {}, "evidence")
    if "visualize_when_applicable" in evidence and not isinstance(evidence["visualize_when_applicable"], bool):
        raise ConfigError("evidence.visualize_when_applicable: expected boolean")
    if "plot_selection_policy" in evidence:
        _required_text(evidence, "plot_selection_policy", "evidence.plot_selection_policy")
    _string_list(evidence.get("artifact_formats", []), "evidence.artifact_formats")
    if "guidance" in evidence:
        _required_text(evidence, "guidance", "evidence.guidance")


def _validate_profile_v2(data: dict[str, Any]) -> None:
    _unknown(data, V2_PROFILE_KEYS, "")
    _equal(data.get("schema_version"), 2, "schema_version")
    _equal(data.get("kind"), "project_profile", "kind")
    _required_text(data, "name", "name")
    source = _section(data.get("source") or {}, "source")
    mode = source.get("workspace_mode", "git_worktree" if source.get("repo_path") else "artifact_directory")
    _optional_text_fields(source, ("repo_path", "default_ref", "workspace_mode"), "source")
    if mode not in {"git_worktree", "artifact_directory"}:
        raise ConfigError("source.workspace_mode: expected 'git_worktree' or 'artifact_directory'")
    runtime = _section(data.get("runtime"), "runtime")
    if runtime.get("backend") not in {"local", "apptainer"}:
        raise ConfigError("runtime.backend: expected 'local' or 'apptainer'")
    _required_text(runtime, "workdir", "runtime.workdir")
    if not str(runtime["workdir"]).startswith("/"):
        raise ConfigError("runtime.workdir: expected absolute container path")
    _required_text(runtime, "command", "runtime.command")
    if "append_agent_args" in runtime and not isinstance(runtime["append_agent_args"], bool):
        raise ConfigError("runtime.append_agent_args: expected boolean")
    if runtime.get("backend") == "apptainer":
        _validate_runtime_apptainer_v2(_section(runtime.get("apptainer") or {}, "runtime_apptainer", "runtime.apptainer"))
    env = _section(runtime.get("env") or {}, "runtime_env", "runtime.env")
    _string_list(env.get("pass", []), "runtime.env.pass")
    if "set" in env and not isinstance(env["set"], dict):
        raise ConfigError("runtime.env.set: expected object")
    for key, value in (env.get("set") or {}).items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ConfigError("runtime.env.set: expected string values")
    _string_list(runtime.get("setup", []), "runtime.setup")
    _string_list(runtime.get("check", []), "runtime.check")
    mounts = _section(runtime.get("mounts") or {}, "runtime_mounts", "runtime.mounts")
    repo_mounts = mounts.get("repo", {})
    if not isinstance(repo_mounts, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in repo_mounts.items()):
        raise ConfigError("runtime.mounts.repo: expected mapping of repo target to host source")
    extra = mounts.get("extra", [])
    if not isinstance(extra, list):
        raise ConfigError("runtime.mounts.extra: expected list")
    for index, item in enumerate(extra):
        mount = _section(item, "runtime_extra_mount", f"runtime.mounts.extra[{index}]")
        _required_text(mount, "source", f"runtime.mounts.extra[{index}].source")
        _required_text(mount, "target", f"runtime.mounts.extra[{index}].target")
        if not str(mount["target"]).startswith("/"):
            raise ConfigError(f"runtime.mounts.extra[{index}].target: expected absolute container path")
        if "mode" in mount and mount["mode"] not in {"ro", "rw"}:
            raise ConfigError(f"runtime.mounts.extra[{index}].mode: expected 'ro' or 'rw'")
    resources = _section(data.get("resources") or {}, "resources")
    for field in ("data_files", "context_paths", "skills", "pre_materialized_lfs_paths", "generated_tracked_paths", "hash_artifacts"):
        _string_list(resources.get(field, []), f"resources.{field}")
    agent = _section(data.get("agent") or {}, "agent")
    _string_list(agent.get("extra_args", []), "agent.extra_args")
    if "timeout_seconds" in agent:
        _positive_int(agent["timeout_seconds"], "agent.timeout_seconds")
    _validate_safety(_section(data.get("safety") or {}, "safety"), "safety")


def _validate_runtime_apptainer_v2(apptainer: dict[str, Any]) -> None:
    _optional_text_fields(apptainer, ("image", "executable", "base_image", "claude_home_template", "claude_host_home", "install_command", "container_claude_dir"), "runtime.apptainer")
    auto_image = apptainer.get("auto_image", None)
    if auto_image is not None and not isinstance(auto_image, bool):
        raise ConfigError("runtime.apptainer.auto_image: expected boolean or null")
    if auto_image is False:
        _required_text(apptainer, "image", "runtime.apptainer.image")
    for field in ("cleanenv", "containall", "writable_tmpfs", "userns", "auto_init_claude_home", "auto_bind_claude_auth"):
        if field in apptainer and not isinstance(apptainer[field], bool):
            raise ConfigError(f"runtime.apptainer.{field}: expected boolean")
    _string_list(apptainer.get("extra_exec_args", []), "runtime.apptainer.extra_exec_args")
    _string_list(apptainer.get("extra_packages", []), "runtime.apptainer.extra_packages")
    for field in ("home_readonly_binds",):
        values = apptainer.get(field, [])
        if not isinstance(values, list):
            raise ConfigError(f"runtime.apptainer.{field}: expected list")


def _compile_runtime_ir(runtime: dict[str, Any], profile_base: Path) -> dict[str, Any]:
    backend = str(runtime["backend"])
    workdir = str(runtime["workdir"]).rstrip("/") or "/"
    command = str(runtime["command"])
    apptainer = deepcopy(runtime.get("apptainer") or {})
    apptainer = _resolve_runtime_apptainer_v2(apptainer, profile_base)
    mounts = _compile_runtime_mounts(runtime.get("mounts") or {}, workdir, profile_base)
    env = runtime.get("env") or {}
    return {
        "backend": backend,
        "workdir": workdir,
        "command": command,
        "append_agent_args": bool(runtime.get("append_agent_args", True)),
        "env": {"pass": list(env.get("pass") or []), "set": dict(env.get("set") or {})},
        "init": [_compile_setup_command(command, index) for index, command in enumerate(runtime.get("setup") or [])],
        "preflight": [{"name": f"check-{index + 1}", "command": command, "required": True} for index, command in enumerate(runtime.get("check") or [])],
        "mounts": mounts,
        "apptainer": apptainer,
    }


def _resolve_runtime_apptainer_v2(apptainer: dict[str, Any], profile_base: Path) -> dict[str, Any]:
    result = deepcopy(apptainer)
    for field in ("image", "claude_home_template", "claude_host_home"):
        if result.get(field):
            result[field] = str(_path(result[field], profile_base))
    result.setdefault("executable", "apptainer")
    return result


def _compile_runtime_mounts(mounts: dict[str, Any], workdir: str, profile_base: Path) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for target, source in (mounts.get("repo") or {}).items():
        source_path = _path(source, profile_base)
        if not source_path.exists():
            raise ConfigError(f"runtime.mounts.repo.{target}: source does not exist: {source_path} (resolved relative to profile_dir)")
        result.append({"source": str(source_path), "target": f"{workdir}/{target.lstrip('/')}", "mode": "ro"})
    for index, item in enumerate(mounts.get("extra") or []):
        source_path = _path(item["source"], profile_base)
        if not source_path.exists():
            raise ConfigError(f"runtime.mounts.extra[{index}].source: source does not exist: {source_path} (resolved relative to profile_dir)")
        result.append({"source": str(source_path), "target": str(item["target"]), "mode": str(item.get("mode") or "ro")})
    return result


def _compile_setup_command(command: str, index: int) -> dict[str, Any]:
    raw = str(command).strip()
    required = True
    if raw.startswith("?"):
        required = False
        raw = raw[1:].strip()
    if not raw:
        raise ConfigError(f"runtime.setup[{index}]: required non-empty command")
    parts = raw.split(None, 1)
    if parts[0] in {"source", "."} and len(parts) == 2:
        path = parts[1].strip()
        op = {"op": "source", "path": path, "required": required}
        if not path.startswith("/"):
            op["base"] = "workdir"
        return op
    return {"op": "shell", "command": raw, "required": required}


def _validate_task(data: dict[str, Any]) -> None:
    _unknown(data, TASK_KEYS, "")
    _equal(data.get("schema_version"), 1, "schema_version")
    _equal(data.get("kind"), "task", "kind")
    task = _section(data.get("task"), "task")
    _required_text(task, "name", "task.name")
    _required_text(task, "goal", "task.goal")
    samples = task.get("samples", [])
    if samples is not None:
        if not isinstance(samples, list):
            raise ConfigError("task.samples: expected list")
        for index, sample in enumerate(samples):
            if not isinstance(sample, dict):
                raise ConfigError(f"task.samples[{index}]: expected object")
            sample_id = sample.get("sample_id")
            if not isinstance(sample_id, str) or not sample_id.strip():
                raise ConfigError(f"task.samples[{index}].sample_id: required non-empty string")

    project = _section(data.get("project"), "project")
    if ("profile" in project) == ("inline" in project):
        raise ConfigError("project: provide exactly one of profile or inline")
    if "profile" in project:
        _required_text(project, "profile", "project.profile")
    if "ref" in project:
        _required_text(project, "ref", "project.ref")
    if "inline" in project and not isinstance(project["inline"], dict):
        raise ConfigError("project.inline: expected object")

    metric = _section(data.get("metric"), "metric")
    _optional_text_fields(metric, ("command", "description", "unit"), "metric")
    _required_text(metric, "name", "metric.name")
    if metric.get("direction") not in {"minimize", "maximize"}:
        raise ConfigError("metric.direction: expected 'minimize' or 'maximize'")
    if not metric.get("command") and not metric.get("description"):
        raise ConfigError("metric: provide at least one of command or description")
    if "repeats" in metric:
        _positive_int(metric["repeats"], "metric.repeats")
    if "improvement" in metric:
        improvement = _section(metric["improvement"], "improvement", "metric.improvement")
        if improvement.get("mode") not in {"absolute", "relative_percent"}:
            raise ConfigError("metric.improvement.mode: expected 'absolute' or 'relative_percent'")
        minimum = improvement.get("minimum")
        if isinstance(minimum, bool) or not isinstance(minimum, (int, float)):
            raise ConfigError("metric.improvement.minimum: expected number")

    validation = _section(data.get("validation") or {"checks": []}, "validation")
    checks = validation.get("checks", [])
    if not isinstance(checks, list):
        raise ConfigError("validation.checks: expected list")
    for index, check in enumerate(checks):
        path = f"validation.checks[{index}]"
        check = _section(check, "check", path)
        _required_text(check, "name", f"{path}.name")
        _required_text(check, "success_criteria", f"{path}.success_criteria")
        if "command" in check:
            _required_text(check, "command", f"{path}.command")

    _validate_safety(_section(data.get("safety") or {}, "safety"), "safety")
    budget = _section(data.get("budget"), "budget")
    _positive_int(budget.get("max_rounds"), "budget.max_rounds", allow_zero=True)
    if "min_rounds" in budget:
        _positive_int(budget["min_rounds"], "budget.min_rounds", allow_zero=True)
    if "patience" in budget:
        _positive_int(budget["patience"], "budget.patience")
    if "candidates_per_round" in budget:
        _positive_int(budget["candidates_per_round"], "budget.candidates_per_round")

    initialization = _section(data.get("initialization") or {}, "initialization")
    if "seed_count" in initialization:
        _positive_int(initialization["seed_count"], "initialization.seed_count")

    generation = _section(data.get("generation") or {}, "generation")
    if "batch_size" in generation:
        _positive_int(generation["batch_size"], "generation.batch_size")
    if "enable_merge" in generation and not isinstance(generation["enable_merge"], bool):
        raise ConfigError("generation.enable_merge: expected boolean")

    gepa = _section(data.get("gepa") or {}, "gepa")
    if "minibatch_size" in gepa:
        _positive_int(gepa["minibatch_size"], "gepa.minibatch_size")
    for field in ("feedback_sample_ids", "pareto_sample_ids"):
        _string_list(gepa.get(field, []), f"gepa.{field}")
    _optional_text_fields(gepa, ("frontier_policy", "acceptance_policy", "parent_sampling"), "gepa")

    judger = _section(data.get("judger") or {}, "judger")
    if "pass_threshold" in judger:
        value = judger["pass_threshold"]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError("judger.pass_threshold: expected number")

    usage_tracking = _section(data.get("usage_tracking") or {}, "usage_tracking")
    for field in ("enabled", "persist_raw_envelope", "print_round_summary", "print_run_summary"):
        if field in usage_tracking and not isinstance(usage_tracking[field], bool):
            raise ConfigError(f"usage_tracking.{field}: expected boolean")

    evidence = _section(data.get("evidence") or {}, "evidence")
    if "visualize_when_applicable" in evidence and not isinstance(evidence["visualize_when_applicable"], bool):
        raise ConfigError("evidence.visualize_when_applicable: expected boolean")
    if "plot_selection_policy" in evidence:
        _required_text(evidence, "plot_selection_policy", "evidence.plot_selection_policy")
    _string_list(evidence.get("artifact_formats", []), "evidence.artifact_formats")
    if "guidance" in evidence:
        _required_text(evidence, "guidance", "evidence.guidance")


def _validate_profile(data: dict[str, Any]) -> None:
    _unknown(data, PROFILE_KEYS, "")
    _equal(data.get("schema_version"), 1, "schema_version")
    _equal(data.get("kind"), "project_profile", "kind")
    _required_text(data, "name", "name")

    source = _section(data.get("source") or {}, "source")
    mode = source.get("workspace_mode", "git_worktree" if source.get("repo_path") else "artifact_directory")
    _optional_text_fields(source, ("repo_path", "default_ref", "workspace_mode"), "source")
    if mode not in {"git_worktree", "artifact_directory"}:
        raise ConfigError("source.workspace_mode: expected 'git_worktree' or 'artifact_directory'")
    environment = _section(data.get("environment") or {}, "environment")
    _string_list(environment.get("setup_commands", []), "environment.setup_commands")
    resources = _section(data.get("resources") or {}, "resources")
    _optional_text_fields(environment, ("description", "python_command", "dependency_policy"), "environment")
    for field in (
        "data_files", "context_paths", "skills", "pre_materialized_lfs_paths",
        "generated_tracked_paths", "hash_artifacts",
    ):
        _string_list(resources.get(field, []), f"resources.{field}")
    assets = resources.get("readonly_assets", [])
    if not isinstance(assets, list):
        raise ConfigError("resources.readonly_assets: expected list")
    for index, asset in enumerate(assets):
        path = f"resources.readonly_assets[{index}]"
        asset = _section(asset, "asset", path)
        _required_text(asset, "source", f"{path}.source")
        _required_text(asset, "target", f"{path}.target")
    agent = _section(data.get("agent") or {}, "agent")
    _string_list(agent.get("extra_args", []), "agent.extra_args")
    _optional_text_fields(agent, ("command",), "agent")
    if "timeout_seconds" in agent:
        _positive_int(agent["timeout_seconds"], "agent.timeout_seconds")
    execution = _section(data.get("execution") or {}, "execution")
    if "max_parallel_candidates" in execution:
        _positive_int(execution["max_parallel_candidates"], "execution.max_parallel_candidates")
    if execution.get("lifecycle", "stateless") not in {"stateless", "materialize_once"}:
        raise ConfigError("execution.lifecycle: expected 'stateless' or 'materialize_once'")
    runtime_backend = execution.get("runtime_backend", "local")
    if runtime_backend not in {"local", "apptainer"}:
        raise ConfigError("execution.runtime_backend: expected 'local' or 'apptainer'")
    if runtime_backend == "apptainer":
        _validate_apptainer(_section(execution.get("apptainer"), "apptainer", "execution.apptainer"))
    elif "apptainer" in execution:
        _validate_apptainer(_section(execution.get("apptainer"), "apptainer", "execution.apptainer"), require_image=False)
    _validate_safety(_section(data.get("safety") or {}, "safety"), "safety")


def _validate_safety(safety: dict[str, Any], path: str) -> None:
    _string_list(safety.get("editable_paths", []), f"{path}.editable_paths")
    _string_list(safety.get("frozen_paths", []), f"{path}.frozen_paths")
    for name in ("max_files_per_candidate", "max_commits_per_candidate"):
        if name in safety:
            _positive_int(safety[name], f"{path}.{name}")


def _validate_apptainer(apptainer: dict[str, Any], require_image: bool = True) -> None:
    auto_image = apptainer.get("auto_image", None)
    if auto_image is not None and not isinstance(auto_image, bool):
        raise ConfigError("execution.apptainer.auto_image: expected boolean or null")
    # `image` is required only when auto-materialization is definitively disabled.
    # When `auto_image` is null (auto) or true, the container_image materializer
    # builds/fills the image at run time, so a missing image is not a schema error.
    effective_require_image = require_image and (auto_image is False)
    if effective_require_image:
        _required_text(apptainer, "image", "execution.apptainer.image")
    _optional_text_fields(
        apptainer,
        (
            "image", "executable", "command", "container_repo", "container_artifacts",
            "container_scratch", "container_home", "claude_home_template",
            "base_image", "container_claude_dir", "install_command",
        ),
        "execution.apptainer",
    )
    for field in ("cleanenv", "containall", "writable_tmpfs", "userns", "auto_bind_claude_auth", "auto_init_claude_home"):
        if field in apptainer and not isinstance(apptainer[field], bool):
            raise ConfigError(f"execution.apptainer.{field}: expected boolean")
    _string_list(apptainer.get("env_allowlist", []), "execution.apptainer.env_allowlist")
    _string_list(apptainer.get("extra_exec_args", []), "execution.apptainer.extra_exec_args")
    _string_list(apptainer.get("extra_packages", []), "execution.apptainer.extra_packages")
    _string_list(apptainer.get("source_scripts", []), "execution.apptainer.source_scripts")
    _string_list(apptainer.get("passthrough_environment", []), "execution.apptainer.passthrough_environment")
    _string_list(apptainer.get("validation_commands", []), "execution.apptainer.validation_commands")
    if "runtime_init" in apptainer:
        runtime_init = _section(apptainer.get("runtime_init"), "runtime_init", "execution.apptainer.runtime_init")
        _string_list(runtime_init.get("setup_commands", []), "execution.apptainer.runtime_init.setup_commands")
        _string_list(runtime_init.get("validation_commands", []), "execution.apptainer.runtime_init.validation_commands")
        _optional_text_fields(runtime_init, ("description", "python_command", "dependency_policy"), "execution.apptainer.runtime_init")
    for field in ("readonly_binds", "extra_binds", "home_readonly_binds"):
        values = apptainer.get(field, [])
        if not isinstance(values, list):
            raise ConfigError(f"execution.apptainer.{field}: expected list")
        for index, value in enumerate(values):
            if isinstance(value, str):
                continue
            bind = _section(value, "bind", f"execution.apptainer.{field}[{index}]")
            _required_text(bind, "source", f"execution.apptainer.{field}[{index}].source")
            _required_text(bind, "target", f"execution.apptainer.{field}[{index}].target")
            if "mode" in bind:
                _required_text(bind, "mode", f"execution.apptainer.{field}[{index}].mode")


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
    frozen = list(dict.fromkeys(
        list(profile.get("frozen_paths") or []) + list(task.get("frozen_paths") or [])
    ))
    result: dict[str, Any] = {"editable_paths": editable, "frozen_paths": frozen}
    for key in ("max_files_per_candidate", "max_commits_per_candidate"):
        values = [int(section[key]) for section in (profile, task) if section.get(key) is not None]
        if values:
            result[key] = min(values)
    return {key: value for key, value in result.items() if value not in (None, [], {})}


def _within(pattern: str, ceilings: list[str]) -> bool:
    if pattern in ceilings:
        return True
    if not any(character in pattern for character in "*?["):
        return any(fnmatch.fnmatch(pattern, ceiling) for ceiling in ceilings)
    return False


def _resolve_resources_v2(resources: dict[str, Any], base: Path) -> dict[str, Any]:
    return {
        "data_files": [str(_path(item, base)) for item in resources.get("data_files", [])],
        "context_paths": [str(_path(item, base)) for item in resources.get("context_paths", [])],
        "skills": list(resources.get("skills") or []),
        "pre_materialized_lfs_paths": list(resources.get("pre_materialized_lfs_paths") or []),
        "generated_tracked_paths": list(resources.get("generated_tracked_paths") or []),
        "hash_artifacts": list(resources.get("hash_artifacts") or []),
    }


def _resolve_resources(resources: dict[str, Any], base: Path) -> dict[str, Any]:
    return {
        "data_files": [str(_path(item, base)) for item in resources.get("data_files", [])],
        "context_paths": [str(_path(item, base)) for item in resources.get("context_paths", [])],
        "skills": list(resources.get("skills") or []),
        "readonly_assets": [
            {"source": str(_path(item["source"], base)), "target": str(item["target"])}
            for item in resources.get("readonly_assets", [])
        ],
        "pre_materialized_lfs_paths": list(resources.get("pre_materialized_lfs_paths") or []),
        "generated_tracked_paths": list(resources.get("generated_tracked_paths") or []),
        "hash_artifacts": list(resources.get("hash_artifacts") or []),
    }


def _resolve_apptainer(apptainer: dict[str, Any], base: Path) -> dict[str, Any]:
    result = deepcopy(apptainer)
    for field in ("image", "claude_home_template", "claude_host_home"):
        if result.get(field):
            result[field] = str(_path(result[field], base))
    for field in ("readonly_binds", "extra_binds", "home_readonly_binds"):
        values = []
        for item in result.get(field, []) or []:
            if isinstance(item, dict):
                values.append({**item, "source": str(_path(item["source"], base))})
            else:
                values.append(item)
        result[field] = values
    return result


def _attach_apptainer_runtime_init(apptainer: dict[str, Any], runtime_contract: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(apptainer)
    configured_init = dict(result.get("runtime_init") or {})
    profile_setup = list(runtime_contract.get("setup_commands") or [])
    explicit_setup = list(configured_init.get("setup_commands") or [])
    configured_init.update(
        {
            "description": runtime_contract.get("description", ""),
            "python_command": runtime_contract.get("python_command", ""),
            "dependency_policy": runtime_contract.get("dependency_policy", ""),
            "setup_commands": list(dict.fromkeys(profile_setup + explicit_setup)),
            "validation_commands": list(configured_init.get("validation_commands") or result.get("validation_commands") or []),
        }
    )
    result["runtime_init"] = configured_init
    return result


def _resolve_git_ref(repo: Path, ref: str) -> str:
    if not repo.exists():
        raise ConfigError(f"source.repo_path: path does not exist: {repo}")
    completed = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", f"{ref}^{{commit}}"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise ConfigError(f"project.ref: cannot resolve {ref!r} in {repo}: {completed.stderr.strip()}")
    return completed.stdout.strip()


def explain_config(config: dict[str, Any]) -> str:
    meta = config.get("_meta") or {}
    lines = ["Resolved GEPA configuration", ""]
    lines.extend(
        f"- source.{label}: {source}"
        for label, source in (meta.get("source_files") or {}).items()
    )
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
        return {
            str(key): "<redacted>"
            if any(part in str(key).lower() for part in sensitive)
            else sanitize_snapshot(item)
            for key, item in value.items()
        }
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


def _section(value: Any, name: str, path: str | None = None) -> dict[str, Any]:
    path = path or name
    if not isinstance(value, dict):
        raise ConfigError(f"{path}: expected object")
    _unknown(value, SECTION_KEYS[name], path)
    return value


def _unknown(data: dict[str, Any], allowed: set[str], path: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        prefix = f"{path}." if path else ""
        raise ConfigError(f"{prefix}{unknown[0]}: unknown field")


def _required_text(data: dict[str, Any], key: str, path: str) -> None:
    if not isinstance(data.get(key), str) or not data[key].strip():
        raise ConfigError(f"{path}: required non-empty string")


def _equal(actual: Any, expected: Any, path: str) -> None:
    if actual != expected:
        raise ConfigError(f"{path}: expected {expected!r}")


def _optional_text_fields(data: dict[str, Any], fields: tuple[str, ...], prefix: str) -> None:
    for field in fields:
        if field in data and (not isinstance(data[field], str) or not data[field].strip()):
            raise ConfigError(f"{prefix}.{field}: expected non-empty string")


def _positive_int(value: Any, path: str, allow_zero: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < (0 if allow_zero else 1):
        operator = ">= 0" if allow_zero else ">= 1"
        raise ConfigError(f"{path}: expected integer {operator}")


def _string_list(value: Any, path: str) -> None:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConfigError(f"{path}: expected list of strings")


def _path(value: Any, base: Path) -> Path:
    path = Path(str(value)).expanduser()
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def _optional_path(value: Any, base: Path) -> Path | None:
    return _path(value, base) if value else None


def _has_dotted(data: dict[str, Any], dotted: str) -> bool:
    current: Any = data
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return False
        current = current[part]
    return True
