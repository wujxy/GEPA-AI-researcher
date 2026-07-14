from __future__ import annotations

from datetime import datetime, timezone
import inspect
from pathlib import Path
from typing import Any

from .agent_client import AgentError, ClaudeCodeClient
from ..config.contracts import format_role_contract
from ..context.prompt_assembler import PromptAssembler
from ..context.views import ContextView
from ..loop.context_views import (
    build_executor_context,
    build_judger_context,
    build_proposer_context,
    candidate_for_executor,
    evidence_access_policy,
)
from ..models.schemas import AgentCallContext, Candidate, CandidateBatch, Judgment, LoopState, SampleTrace, Trace


class AgentProtocolError(ValueError):
    """Agent returned parseable JSON that does not satisfy GEPA's hard protocol."""


def format_runtime(config: dict[str, Any]) -> str:
    runtime = config.get("runtime", {})
    if not runtime:
        return "Runtime envelope:\n- Not specified; inspect the project and available resources."

    lines = ["Runtime environment:"]
    environment = runtime.get("environment")
    conda_env = runtime.get("conda_env")
    python_command = runtime.get("python_command")
    dependency_policy = runtime.get("dependency_policy")
    allowed_commands = runtime.get("allowed_commands", [])
    guarantee = runtime.get("guarantee")

    if environment:
        lines.append(f"- Environment type: {environment}")
    if conda_env:
        lines.append(f"- Conda environment: {conda_env}")
    if python_command:
        lines.append(f"- Python command: {python_command}")
    if dependency_policy:
        lines.append(f"- Dependency policy: {dependency_policy}")
    if guarantee:
        lines.append(f"- Guarantee: {guarantee}")
    if allowed_commands:
        lines.append(f"- Reference commands from legacy config (not mandatory): {allowed_commands}")
    lines.append("- Reference commands and environment notes are hints/context, not GEPA-enforced steps.")
    lines.append("- Do not install new packages during the loop.")
    lines.append("- If a package is unavailable, record the import error and fall back to a simpler available method.")
    return "\n".join(lines)


def format_evidence_policy(config: dict[str, Any]) -> str:
    evidence = config.get("evidence", {})
    if not evidence:
        return "Visual evidence:\n- No explicit visual evidence policy configured."

    lines = ["Visual evidence:"]
    if evidence.get("visualize_when_applicable", False):
        lines.append("- When the task can be explained or validated visually, create plot artifacts whenever feasible.")
    if evidence.get("plot_selection_policy") == "proposer_selects":
        lines.append("- The proposer should choose task-appropriate plots; do not assume a fixed plot set for every task.")
    formats = evidence.get("artifact_formats", [])
    if formats:
        lines.append(f"- Preferred artifact formats: {formats}")
    guidance = evidence.get("guidance")
    if guidance:
        lines.append(f"- Guidance: {guidance}")
    lines.append("- Save visual artifacts under the provided working directory and include their paths in artifact_paths.")
    lines.append("- If plotting is not possible in the runtime, explain why in errors or diagnostics.")
    return "\n".join(lines)


def format_proposer_context(context: dict[str, Any]) -> str:
    if not context:
        return "Proposer role context:\n- No prior candidate pool exists yet; create seed candidate(s)."
    prior_context = context.get("prior_context") or {}
    return (
        "Prior context:\n"
        f"- Notes: {prior_context.get('notes', [])}\n"
        f"- Skills: {prior_context.get('skills', [])}\n"
        f"- Documents: {prior_context.get('documents', [])}\n"
        f"- Warnings: {prior_context.get('warnings', [])}\n\n"
        "Proposer role context:\n"
        f"- Frontier: {context.get('frontier', {})}\n"
        f"- Parent candidates: {context.get('parents', [])}\n"
        f"- Score summary: {context.get('score_summary', {})}\n"
        f"- Recent feedback: {context.get('recent_feedback', [])}\n"
        f"- Recent traces: {context.get('recent_traces', [])}\n"
        f"- Dataset split: {context.get('dataset_split', {})}\n\n"
        "Current state facts:\n"
        f"{context.get('state', {})}"
    )


def format_executor_context(context: dict[str, Any]) -> str:
    prior_context = context.get("prior_context") or {}
    evaluation = context.get("evaluation") or {}
    workspace = context.get("workspace") or {}
    return (
        "Prior context:\n"
        f"- Notes: {prior_context.get('notes', [])}\n"
        f"- Skills: {prior_context.get('skills', [])}\n"
        f"- Documents: {prior_context.get('documents', [])}\n"
        f"- Warnings: {prior_context.get('warnings', [])}\n\n"
        f"Evaluation phase: {evaluation.get('eval_phase', 'pareto')}\n"
        f"Execution mode: {evaluation.get('execution_mode')}\n"
        f"Selected sample ids: {evaluation.get('selected_sample_ids', [])}\n\n"
        "Candidate decision facts:\n"
        f"{context.get('candidate', {})}\n\n"
        "Working directory for any scripts/artifacts you create:\n"
        f"{workspace.get('artifact_dir')}\n\n"
        "Candidate source repository:\n"
        f"{workspace.get('source_repo')}"
    )


def format_judger_context(context: dict[str, Any]) -> str:
    evaluation = context.get("evaluation") or {}
    return (
        "Candidate decision facts:\n"
        f"{context.get('candidate', {})}\n\n"
        "Trace decision facts:\n"
        f"{context.get('trace', {})}\n\n"
        f"Evaluation phase: {evaluation.get('eval_phase', 'pareto')}\n"
        f"Selected sample ids: {evaluation.get('selected_sample_ids', [])}"
    )


def format_task_resources(config: dict[str, Any]) -> str:
    task = config.get("task", {})
    resource_fields = {
        "data_files": task.get("data_files", []),
        "repo_paths": task.get("repo_paths", []),
        "workspaces": task.get("workspaces", []),
        "benchmark_commands": task.get("benchmark_commands", []),
        "validation_commands": task.get("validation_commands", []),
        "artifacts": task.get("artifacts", []),
    }
    resource_fields = {key: value for key, value in resource_fields.items() if value}
    if not resource_fields:
        return "Task resources:\n- No task resources configured."
    lines = ["Task resources:"]
    for key, value in resource_fields.items():
        lines.append(f"- {key}: {value}")
    return "\n".join(lines)


def format_candidate_policy(config: dict[str, Any]) -> str:
    policy = config.get("candidate_policy", {})
    if not policy:
        return "Candidate policy:\n- No deterministic admission policy configured."
    lines = ["Candidate policy:"]
    baseline_ref = config.get("workspace", {}).get("baseline_ref")
    if baseline_ref:
        lines.append(f"- Source baseline/ref: {baseline_ref}")
    known_targets = list(policy.get("known_target_files", []))
    if known_targets:
        lines.append("- target_files must be copied exactly from this known source list when applicable:")
        for target in known_targets:
            lines.append(f"  - {target}")
    allowed_globs = policy.get("allowed_target_globs", [])
    if allowed_globs:
        lines.append(f"- allowed_target_globs: {allowed_globs}")
    frozen = policy.get("frozen_globs", [])
    if frozen:
        lines.append(f"- frozen_globs: {frozen}")
    max_targets = policy.get("max_target_files")
    if max_targets is not None:
        lines.append(f"- max_target_files: {max_targets}")
    return "\n".join(lines)


def format_config_for_role(config: dict[str, Any], role: str) -> str:
    contract = format_role_contract(config, role)
    if contract:
        return contract
    if role == "judger":
        return ""
    return "\n\n".join([
        format_task_resources(config),
        format_candidate_policy(config),
        format_runtime(config),
    ])


def _candidate_prompt_text(data: dict[str, Any]) -> str:
    strategy = data.get("strategy") or data.get("approach") or "unspecified"
    return (
        f"Strategy: {strategy}\n"
        f"Hypothesis: {data.get('hypothesis', '')}\n"
        f"Plan: {data.get('analysis_plan', [])}"
    )


def _run_agent_json(client, prompt: str, label: str, context: AgentCallContext, **kwargs):
    parameters = inspect.signature(client.run_json).parameters
    if "call_context" in parameters:
        return client.run_json(prompt, label=label, call_context=context, **kwargs)
    return client.run_json(prompt, label=label)


def _expected_gain(data: dict[str, Any]) -> float | None:
    # Only use the generic expected_gain field - removed OMILREC-specific expected_gain_ms_evt
    # This makes GEPA framework task-agnostic and avoids OMILREC specific logic
    value = data.get("expected_gain")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _call_artifact(result) -> dict[str, Any]:
    record = getattr(result, "call_record", None)
    return {"agent_call_id": record.call_id} if record is not None else {}


def _require_mapping(data: Any, label: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise AgentProtocolError(f"{label}: expected JSON object")
    return data


def _require_fields(data: dict[str, Any], fields: tuple[str, ...], label: str) -> None:
    missing = [field for field in fields if field not in data or data[field] in (None, "")]
    if missing:
        raise AgentProtocolError(f"missing required {label} field(s): {missing}")


def _require_list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise AgentProtocolError(f"{field}: expected list")
    return value


def validate_proposal_payload(data: Any) -> dict[str, Any]:
    payload = _require_mapping(data, "proposer payload")
    _require_fields(
        payload,
        ("hypothesis", "scope", "proposed_change", "rationale", "expected_improvement", "risk"),
        "proposer",
    )
    for field in ("target_files", "expected_artifacts", "analysis_plan"):
        if field in payload:
            _require_list(payload[field], field)
    if "executor_contract" in payload and not isinstance(payload["executor_contract"], dict):
        raise AgentProtocolError("executor_contract: expected object")
    return payload


def validate_executor_payload(data: Any) -> dict[str, Any]:
    payload = _require_mapping(data, "executor payload")
    _require_fields(
        payload,
        ("summary", "implementation", "metrics", "validation", "diagnostics", "artifact_paths", "errors"),
        "executor",
    )
    if not isinstance(payload["metrics"], dict):
        raise AgentProtocolError("metrics: expected object")
    if not isinstance(payload["validation"], dict):
        raise AgentProtocolError("validation: expected object")
    _require_list(payload["diagnostics"], "diagnostics")
    _require_list(payload["artifact_paths"], "artifact_paths")
    _require_list(payload["errors"], "errors")
    return payload


def validate_judgment_payload(data: Any) -> dict[str, Any]:
    payload = _require_mapping(data, "judger payload")
    _require_fields(
        payload,
        ("score", "passed", "per_sample_scores", "failure_categories", "actionable_feedback", "confidence"),
        "judger",
    )
    try:
        score = float(payload["score"])
    except (TypeError, ValueError) as exc:
        raise AgentProtocolError("score: expected number") from exc
    if not 0.0 <= score <= 1.0:
        raise AgentProtocolError("score: expected value between 0.0 and 1.0")
    if not isinstance(payload["passed"], bool):
        raise AgentProtocolError("passed: expected boolean")
    for field in ("per_sample_scores", "failure_categories", "actionable_feedback"):
        _require_list(payload[field], field)
    if str(payload["confidence"]) not in {"low", "medium", "high"}:
        raise AgentProtocolError("confidence: expected low, medium, or high")
    return payload


# Shared by the executor's normal and repair prompts so the two cannot drift.
_EXECUTOR_RESULT_SCHEMA = """{
  "summary": "what you executed",
  "implementation": {"changed_files": [], "commands_run": [], "commit_sha": null, "committed_files": [], "git_status_after_commit": "", "notes": ""},
  "metrics": {"primary": null, "baseline": null, "delta": null},
  "validation": {"passed": false, "checks": [], "regressions": []},
  "diagnostics": ["diagnostic or failure finding"],
  "artifact_paths": ["relative or absolute paths"],
  "errors": []
}"""


_PROPOSER_CANDIDATE_SCHEMA = """{
  "hypothesis": "short falsifiable hypothesis or optimization idea",
  "scope": "module, component, prompt, dataset, workflow, or subsystem to change/test",
  "proposed_change": "what to test this round",
  "rationale": "why this is a good next candidate",
  "expected_improvement": "which configured metric or objective should improve",
  "risk": "main risk or failure mode",
  "strategy": "short name for the approach",
  "target_files": ["file to change"],
  "safety_class": "optional short safety note (free text)",
  "candidate_class": "optional optimization-type tag (free text)",
  "expected_gain": 0.0,
  "analysis_plan": ["step 1", "step 2"],
  "executor_contract": {"instructions": "what the executor should attempt or verify", "expected_artifacts": ["artifact"], "success_criteria": ["criterion"]},
  "expected_artifacts": ["artifact"],
  "mutation_note": "what prior feedback this candidate responds to"
}"""


_PROPOSER_FINAL_CONTRACT = """Final delivery contract (mandatory):
- Your final response MUST be exactly one parseable JSON object matching the required schema below.
- Wrapping the JSON in a ```json code fence is acceptable; any prose, Markdown table, heading, commentary, status update, or natural-language wrap-up outside the JSON is forbidden.
- NEVER say that you have already submitted candidates, that you will continue later, or that another agent completed work unless that text is inside a JSON string field.
- If you are uncertain, blocked, or found that an idea is infeasible, still return the required JSON object with conservative fields, explicit risk, and an analysis_plan step that verifies the uncertainty.
- Do not ask for more data and do not emit a natural-language summary."""


def _proposer_context(state: LoopState, config: dict[str, Any]) -> ContextView | dict[str, Any]:
    context_view = _context_view_from_config(config)
    if context_view is not None:
        return context_view
    return build_proposer_context(state, config)


def _context_view_from_config(config: dict[str, Any]) -> ContextView | None:
    context_view = config.get("_context_view")
    if isinstance(context_view, ContextView):
        return context_view
    if isinstance(context_view, dict) and {"role", "envelope", "blocks"} <= context_view.keys():
        return ContextView.from_dict(context_view)
    return None


def _proposer_parent_ids(proposer_context: ContextView | dict[str, Any], config: dict[str, Any]) -> list[str]:
    metadata = proposer_context.metadata if isinstance(proposer_context, ContextView) else proposer_context.get("metadata") or {}
    parent_ids = metadata.get("parent_ids")
    if parent_ids is not None:
        return list(parent_ids)
    return list((config.get("_gepa_context") or {}).get("pareto_frontier", {}).get("parent_ids", []))


class AgentProposer:
    def __init__(self, client: ClaudeCodeClient):
        self.client = client

    def propose(self, state: LoopState, config: dict[str, Any]) -> Candidate:
        proposer_context = _proposer_context(state, config)
        legacy_proposer_context = {} if isinstance(proposer_context, ContextView) else proposer_context
        prompt = f"""
You are the PROPOSER agent in a bounded GEPA-style research loop.

Task goal:
{config["task"]["goal"]}

{format_config_for_role(config, "proposer")}

{format_evidence_policy(config)}

{format_proposer_context(legacy_proposer_context)}

{evidence_access_policy()}

Important constraints:
- Propose exactly one candidate research hypothesis or intervention for the next round.
- If parent candidates are provided, mutate from them instead of starting from scratch.
- Include an executor_contract that suggests what the executor should attempt and what evidence to return.
- Do not assume hidden task facts that are not present in resources, prior context, or loop feedback.
- Use only the configured resources and prior loop feedback.
- Keep the candidate small enough for the executor to test in one round.
- Propose candidates that are executable in the runtime environment above.
- Include diagnostics or evidence artifacts in the analysis plan when they can support or falsify the candidate.
- Choose task-appropriate evidence; do not rely on a fixed artifact template.
{_PROPOSER_FINAL_CONTRACT}

Required JSON schema:
{_PROPOSER_CANDIDATE_SCHEMA}
"""
        prompt = PromptAssembler().build_proposer_prompt(state, config, proposer_context)
        call_context = AgentCallContext(
            role="proposer",
            round_id=state.round_id,
            phase=str(config.get("_agent_phase", "mutation")),
            candidate_ids=[f"cand_{state.round_id:03d}"],
        )
        result, repair_meta = self._run_with_repair(
            prompt,
            call_context,
            config,
            repair_schema=_PROPOSER_CANDIDATE_SCHEMA,
        )
        data = validate_proposal_payload(result.data)
        candidate_id = f"cand_{state.round_id:03d}"
        prompt_text = _candidate_prompt_text(data)
        return Candidate(
            candidate_id=candidate_id,
            round_id=state.round_id,
            hypothesis=str(data.get("hypothesis", "")),
            scope=str(data.get("scope", "task_system")),
            proposed_change=str(data.get("proposed_change", "")),
            rationale=str(data.get("rationale", "")),
            expected_improvement=str(data.get("expected_improvement", "")),
            risk=str(data.get("risk", "")),
            prompt_text=prompt_text,
            created_at=datetime.now(timezone.utc).isoformat(),
            parent_ids=_proposer_parent_ids(proposer_context, config),
            executor_contract=dict(data.get("executor_contract", {})),
            expected_artifacts=list(data.get("expected_artifacts", [])),
            mutation_note=str(data.get("mutation_note", "")),
            target_files=list(map(str, data.get("target_files", []))),
            safety_class=str(data.get("safety_class", "")),
            strategy=str(data.get("strategy", "")),
            candidate_class=str(data.get("candidate_class", "")),
            expected_gain=_expected_gain(data),
            artifacts={"agent_raw": result.text, "eval_phase": config.get("_eval_phase", "pareto"), "sample_ids": config.get("_selected_sample_ids", []), **_call_artifact(result), **data, **repair_meta},
        )

    def propose_batch(self, state: LoopState, config: dict[str, Any]) -> CandidateBatch:
        batch_size = int(config.get("generation", {}).get("batch_size", 10))
        proposer_context = _proposer_context(state, config)
        legacy_proposer_context = {} if isinstance(proposer_context, ContextView) else proposer_context
        prompt = f"""
You are the PROPOSER agent in a bounded GEPA-style research loop.

Task goal:
{config["task"]["goal"]}

{format_config_for_role(config, "proposer")}

{format_evidence_policy(config)}

{format_proposer_context(legacy_proposer_context)}

{evidence_access_policy()}

Important constraints:
- Propose exactly {batch_size} candidate research hypotheses or interventions for the next generation.
- If parent candidates are provided, each proposal must be a reflective mutation of the Pareto frontier parent(s).
- Make the candidates meaningfully diverse while staying grounded in parent feedback.
- Include executor_contract and expected_artifacts for every candidate.
- Do not assume hidden task facts that are not present in resources, prior context, or loop feedback.
- Use only the configured resources and prior loop feedback.
- Keep each candidate small enough for the executor to test in one isolated workspace.
- Propose candidates that are executable in the runtime environment above.
- Include diagnostics or evidence artifacts in each analysis plan when they can support or falsify the candidate.
{_PROPOSER_FINAL_CONTRACT}

Required JSON schema:
{{
  "candidates": [
    {_PROPOSER_CANDIDATE_SCHEMA}
  ]
}}
"""
        prompt = PromptAssembler().build_proposer_prompt(state, config, proposer_context, batch_size=batch_size)
        planned_ids = [f"cand_{state.round_id:03d}_{index:03d}" for index in range(batch_size)]
        call_context = AgentCallContext(
            role="proposer",
            round_id=state.round_id,
            phase=str(config.get("_agent_phase", "mutation")),
            candidate_ids=planned_ids,
        )
        result, repair_meta = self._run_with_repair(
            prompt,
            call_context,
            config,
            repair_schema='{"candidates": [' + _PROPOSER_CANDIDATE_SCHEMA + ']}',
        )
        batch_payload = _require_mapping(result.data, "proposer batch payload")
        items = _require_list(batch_payload.get("candidates"), "candidates")[:batch_size]
        parent_ids = _proposer_parent_ids(proposer_context, config)
        candidates = []
        for index, data in enumerate(items):
            data = validate_proposal_payload(data)
            candidate_id = f"cand_{state.round_id:03d}_{index:03d}"
            prompt_text = _candidate_prompt_text(data)
            candidates.append(
                Candidate(
                    candidate_id=candidate_id,
                    round_id=state.round_id,
                    hypothesis=str(data.get("hypothesis", "")),
                    scope=str(data.get("scope", "task_system")),
                    proposed_change=str(data.get("proposed_change", "")),
                    rationale=str(data.get("rationale", "")),
                    expected_improvement=str(data.get("expected_improvement", "")),
                    risk=str(data.get("risk", "")),
                    prompt_text=prompt_text,
                    created_at=datetime.now(timezone.utc).isoformat(),
                    parent_ids=parent_ids,
                    executor_contract=dict(data.get("executor_contract", {})),
                    expected_artifacts=list(data.get("expected_artifacts", [])),
                    mutation_note=str(data.get("mutation_note", "")),
                    target_files=list(map(str, data.get("target_files", []))),
                    safety_class=str(data.get("safety_class", "")),
                    strategy=str(data.get("strategy", "")),
                    candidate_class=str(data.get("candidate_class", "")),
                    expected_gain=_expected_gain(data),
                    artifacts={"agent_raw": result.text, "eval_phase": config.get("_eval_phase", "pareto"), "sample_ids": config.get("_selected_sample_ids", []), **_call_artifact(result), **data, **repair_meta},
                )
            )
        return CandidateBatch(round_id=state.round_id, candidates=candidates)

    def _run_with_repair(
        self,
        prompt: str,
        call_context: AgentCallContext,
        config: dict[str, Any],
        *,
        repair_schema: str,
    ):
        repair_retries = int(config.get("proposer", {}).get("repair_retries", 1))
        try:
            return _run_agent_json(self.client, prompt, "proposer", call_context), {}
        except AgentError as exc:
            if repair_retries <= 0:
                raise
            raw_output = str(getattr(exc, "raw_output", None) or exc)[:6000]
            repair_prompt = self._repair_prompt(raw_output, repair_schema)
            result = _run_agent_json(self.client, repair_prompt, "proposer", call_context)
            return result, {"repair_applied": True, "original_raw_output": raw_output}

    def _repair_prompt(self, raw_output: str, schema: str) -> str:
        return (
            "You are the PROPOSER agent in a bounded GEPA-style research loop. "
            "A PREVIOUS proposer attempt returned malformed JSON. Your ONLY job now is to transcribe "
            "that prior proposal into valid JSON. DO NOT invent new candidates, new strategies, "
            "new metrics, or new evidence. Preserve the same meaning and candidate count from the raw output "
            "as closely as possible.\n\n"
            "Raw malformed output:\n"
            f"{raw_output}\n\n"
            "Mandatory output rules:\n"
            "- Return exactly one parseable JSON object.\n"
            "- No prose outside JSON.\n"
            "- Merge accidentally split instruction strings back into executor_contract.instructions.\n"
            "- The repaired JSON must match this schema:\n"
            f"{schema}"
        )


class AgentExecutor:
    def __init__(self, client: ClaudeCodeClient, run_dir: Path):
        self.client = client
        self.run_dir = run_dir

    def execute(self, candidate: Candidate, config: dict[str, Any]) -> Trace:
        visible_round_dir = Path(
            config.get("_candidate_workspace")
            or self.run_dir / "agent_work" / f"round_{candidate.round_id:03d}" / candidate.candidate_id
        )
        host_round_dir = Path(config.get("_candidate_workspace_host") or visible_round_dir)
        host_round_dir.mkdir(parents=True, exist_ok=True)
        visible_repo_dir = Path(config.get("_candidate_repo") or getattr(self.client, "cwd", None) or visible_round_dir)
        host_cwd_value = config.get("_executor_host_cwd") or config.get("_candidate_repo_host")
        host_cwd = Path(host_cwd_value) if host_cwd_value else visible_repo_dir
        execution_mode = str(config.get("_execution_mode", "implement_and_validate"))
        executor_context = _context_view_from_config(config) or build_executor_context(candidate, config, self.run_dir, visible_round_dir, visible_repo_dir, execution_mode)
        legacy_executor_context = {} if isinstance(executor_context, ContextView) else executor_context
        prompt = f"""
You are the EXECUTOR agent in a bounded GEPA-style research loop.

You may inspect files and run commands inside the repository. Your job is to
execute the proposed candidate under the configured task resources and return
structured evidence about what happened.

Task goal:
{config["task"]["goal"]}

{format_config_for_role(config, "executor")}

{format_evidence_policy(config)}

{format_executor_context(legacy_executor_context)}

{evidence_access_policy()}

Constraints:
- Do not ask the user for help.
- Do not assume hidden task facts that are not present in resources, prior context, or loop feedback.
- Runnable envelope guidance:
  - The user guarantees the declared source path, docs, provided paths, and reference commands are sufficient to run the project.
  - Prefer the task metric, validation, and reference commands as the known-good build/test/benchmark path before inventing alternatives.
  - Reference commands are hints/context, not GEPA-enforced steps; adapt them when the repository or candidate requires it.
  - In Apptainer runs, the project runtime comes from host-runtime passthrough plus provided paths; the image is only a startup shell/agent boundary.
  - Preserve task-provided environment variables and data/resource paths when measuring or validating; do not silently fall back to older paths from historical docs.
  - Use the project build system before ad hoc compiler commands. If you must deviate, explain why and mark any affected validation as incomplete.
  - If a Python test entrypoint fails after sourcing a project environment, try the project/host pytest executable before declaring pytest unavailable.
  - If a required validation gate is skipped, report the candidate as incomplete rather than fully validated.
- When a command fails, distinguish infrastructure failure from command-selection failure; report the exact command, cwd, relevant env vars, and stderr excerpt.
- Keep this execution compact and scoped to the candidate contract.
- Avoid broad repository exploration unless the candidate contract requires it.
- You may inspect files, make bounded changes, run validation or benchmark commands,
  and compare against available baselines when useful for this candidate.
- Save any scripts or generated artifacts under the working directory above.
- In implement_and_validate mode, edit only admitted target_files.
- In implement_and_validate mode, you MUST create a Git commit for the candidate source changes before your final JSON response. The orchestrator reads HEAD as the candidate result revision; uncommitted edits are treated as no implementation.
- Before committing, run git status --porcelain and git diff --name-only. Stage only admitted target_files with git add -- <target_files>; do not stage build outputs, benchmark logs, fixtures, caches, or other runtime artifacts.
- Run git commit with a concise candidate-scoped message, then run git rev-parse HEAD and put that SHA in implementation.commit_sha. Also report implementation.committed_files and implementation.git_status_after_commit.
- If you cannot create the commit, set validation.passed=false, leave implementation.commit_sha=null, and explain the exact git/status failure in errors and diagnostics.
- In evaluate_only mode, do not edit source files, create commits, switch branches, or change HEAD.
- Never run git checkout, git switch, or git worktree; the orchestrator owns Git lifecycle.
- When visual evidence is feasible, follow the candidate's visual evidence plan.
  Save plot file(s) under the working directory and list them in artifact_paths.

Final delivery contract (mandatory):
- Your final response MUST be exactly one parseable JSON object matching the schema below.
- Wrapping the JSON in a ```json code fence is acceptable; any prose, status updates, apologies, commentary, or natural-language wrap-up outside the JSON is forbidden.
- NEVER finish with natural-language status such as "waiting for results", "still running", "will continue", or "need more time".
- Do NOT run benchmark/validation/test/build commands in the background. Run them in the foreground and block until they exit, then parse their output before responding.
- If a command is still running, wait for it to finish unless the configured timeout forces you to stop.
- If execution is incomplete, blocked, interrupted, or a command produced no metric, you MUST still return the JSON object with validation.passed=false, null metrics, the reason in errors, and the partial state in diagnostics/artifact_paths.
- Metric evidence must come from fresh foreground execution for this candidate and this phase. If the task metric specifies repeats, run the metric command for the configured repeat count unless the timeout or an explicit command failure prevents it. Do not use historical logs, pre-existing TEMP files, old benchmark outputs, or accumulated prior-candidate results as the primary metric.
- If you cannot freshly run the required metric repeats, set validation.passed=false, set missing metrics to null, and explain the partial evidence in diagnostics/errors.
- A partial or failed run is acceptable ONLY if it is reported as JSON.
- Set validation.passed=true only when all required validation and metric gates actually passed with fresh evidence for this candidate.

Required JSON schema:
{_EXECUTOR_RESULT_SCHEMA}
"""
        prompt = PromptAssembler().build_executor_prompt(candidate, config, executor_context)
        client = self._client_for_config(config)
        call_context = AgentCallContext(
            role="executor",
            round_id=candidate.round_id,
            phase=str(config.get("_eval_phase", "pareto")),
            candidate_id=candidate.candidate_id,
            execution_id=config.get("_execution_id"),
            parent_candidate_id=candidate.parent_ids[0] if candidate.parent_ids else None,
        )
        repair_retries = int(config.get("executor", {}).get("repair_retries", 1))
        repair_meta: dict[str, Any] = {}
        try:
            result = _run_agent_json(
                client,
                prompt,
                "executor",
                call_context,
                cwd=host_cwd,
                env=dict(config.get("_candidate_env") or {}),
                command_prefix=list(config.get("_executor_command_prefix") or []),
                inherit_host_env=bool(config.get("_executor_inherit_host_env", True)),
                resolve_command_on_host=bool(config.get("_executor_resolve_command_on_host", True)),
            )
        except AgentError as exc:
            # The agent returned no parseable JSON (typically it stopped early and
            # narrated status). Before recording a terminal infrastructure failure,
            # give the same agent ONE cheap "transcribe the current state into JSON"
            # call — it must NOT re-run the task, just summarize what already exists.
            if repair_retries <= 0:
                raise
            raw_output = str(getattr(exc, "raw_output", None) or exc)[:4000]
            candidate_json_path = str(
                self.run_dir / "traces" / f"round_{candidate.round_id:03d}" / candidate.candidate_id / "candidate.json"
            )
            repair_prompt = self._repair_prompt(
                candidate=candidate,
                raw_output=raw_output,
                repo_dir=visible_repo_dir,
                round_dir=visible_round_dir,
                candidate_json_path=candidate_json_path,
            )
            repair_client = self._repair_client_for_config(config)
            result = _run_agent_json(
                repair_client,
                repair_prompt,
                "executor",
                call_context,
                cwd=host_cwd,
                env=dict(config.get("_candidate_env") or {}),
                command_prefix=list(config.get("_executor_command_prefix") or []),
                inherit_host_env=bool(config.get("_executor_inherit_host_env", True)),
                resolve_command_on_host=bool(config.get("_executor_resolve_command_on_host", True)),
            )
            repair_meta = {"repair_applied": True, "original_raw_output": raw_output}
        data = validate_executor_payload(result.data)
        trace = SampleTrace(
            sample_id=str((config.get("_selected_sample_ids") or ["task_execution"])[0]),
            input=str(config.get("task", {})),
            output=str(data),
            expected="unknown",
            logs=str(data.get("summary", "")),
            error="; ".join(data.get("errors", [])) if data.get("errors") else None,
            artifacts={"agent_raw": result.text, "eval_phase": config.get("_eval_phase", "pareto"), "sample_ids": config.get("_selected_sample_ids", []), "execution_mode": execution_mode, **_call_artifact(result), **data, **repair_meta},
        )
        return Trace(candidate_id=candidate.candidate_id, round_id=candidate.round_id, samples=[trace])

    def _client_for_config(self, config: dict[str, Any]) -> ClaudeCodeClient:
        timeout = config.get("_executor_timeout_seconds")
        if timeout is None:
            return self.client
        return ClaudeCodeClient(
            command=str(config.get("_executor_command") or self.client.command),
            cwd=self.client.cwd,
            timeout_seconds=int(timeout),
            extra_args=list(self.client.extra_args),
            heartbeat_seconds=self.client.heartbeat_seconds,
            usage_tracker=self.client.usage_tracker,
        )

    def _repair_client_for_config(self, config: dict[str, Any]) -> ClaudeCodeClient:
        # The repair call only transcribes existing state into JSON (it is told
        # not to run commands), so cap its timeout well below the executor budget.
        # Reuse injected non-ClaudeCodeClient clients as-is so tests can drive the
        # repair path with fakes.
        if not isinstance(self.client, ClaudeCodeClient):
            return self.client
        executor_timeout = config.get("_executor_timeout_seconds")
        if executor_timeout is None:
            executor_timeout = self.client.timeout_seconds
        repair_timeout = int(config.get("executor", {}).get("repair_timeout_seconds", 600))
        timeout_seconds = min(int(executor_timeout), repair_timeout)
        return ClaudeCodeClient(
            command=str(config.get("_executor_command") or self.client.command),
            cwd=self.client.cwd,
            timeout_seconds=timeout_seconds,
            extra_args=list(self.client.extra_args),
            heartbeat_seconds=self.client.heartbeat_seconds,
            usage_tracker=self.client.usage_tracker,
        )

    def _repair_prompt(
        self,
        candidate: Candidate,
        raw_output: str,
        repo_dir: Path,
        round_dir: Path,
        candidate_json_path: str,
    ) -> str:
        candidate_facts = candidate_for_executor(candidate, [candidate_json_path])
        return f"""
You are the EXECUTOR agent in a bounded GEPA-style research loop. A PREVIOUS attempt
at this exact task finished WITHOUT returning a parseable JSON object. Your ONLY job now
is to transcribe the current state into the JSON schema below. DO NOT continue the task.

Mandatory - DO NOT:
- Run any new build, benchmark, validation, test, or long-running command.
- Edit source files, create commits, switch branches, or change HEAD.
- Re-attempt the optimization.

You MAY (only to reconstruct what already happened):
- Read files and run read-only git (git log / git diff / git status / git show) inside the
  source worktree.
- List/read files under the working/artifact directory.

Inputs from the previous attempt:
- Previous (non-JSON) raw output:
{raw_output}

- Candidate facts:
{candidate_facts}

- Source worktree: {repo_dir}
- Working/artifact directory: {round_dir}

Produce EXACTLY one JSON object matching this schema, describing the state the previous
attempt left behind:
{_EXECUTOR_RESULT_SCHEMA}

Set validation.passed=false if any gate did not actually pass; use null for any metric
never produced; put the reason in errors; list files/paths the previous attempt created in
artifact_paths; summarize what was and was not completed in diagnostics. Return only the
JSON object.
"""


class AgentJudger:
    def __init__(self, client: ClaudeCodeClient):
        self.client = client

    def judge(self, candidate: Candidate, trace: Trace, config: dict[str, Any]) -> Judgment:
        judger_context = _context_view_from_config(config) or build_judger_context(candidate, trace, config)
        legacy_judger_context = {} if isinstance(judger_context, ContextView) else judger_context
        call_context = AgentCallContext(
            role="judger",
            round_id=candidate.round_id,
            phase=str(config.get("_eval_phase", "pareto")),
            candidate_id=candidate.candidate_id,
            execution_id=config.get("_execution_id"),
            parent_candidate_id=candidate.parent_ids[0] if candidate.parent_ids else None,
        )
        prompt = f"""
You are the JUDGER agent in a bounded GEPA-style research loop.

Evaluate whether the executor's result supports the candidate as a useful
improvement or valid finding for the configured task.

Task goal:
{config["task"]["goal"]}

{format_config_for_role(config, "judger")}

{format_judger_context(legacy_judger_context)}

{evidence_access_policy()}

Rubric:
- Score 0.0 to 1.0 by comparing the executor evidence against the user's stated task goal, metric direction, improvement target if present, validation contract, and practical ambition.
- Passing validation gates is necessary for a high score when those gates are relevant, but it is not sufficient for 0.95-1.00. Do not assign 1.0 merely because all gates are green.
- 0.95-1.00: exceptional / exceeds goal. The result clearly exceeds the user goal, is far better than the stated requirement, or achieves the strongest plausible version of the goal with complete, robust evidence. Reserve 1.00 for near-ideal results that would be hard to improve within the current loop.
- 0.80-0.95: strong / clearly satisfies goal. The result satisfies the main user goal well, has meaningful task-relative impact, convincing metrics, and relevant validation.
- 0.70-0.80: good / useful satisfaction. The result satisfies the goal in a useful way, but effect size, robustness, novelty, or validation completeness is only moderate.
- 0.60-0.70: valid but weak / needs follow-up. The result is directionally useful and plausibly satisfies the task, but impact is modest or confidence/validation is limited.
- 0.50-0.60: barely useful / noisy or marginal. The result is aligned with the goal, but the measured effect may be within noise, too small to matter, or weakly supported.
- 0.35-0.50: partial / inconclusive. Some implementation or analysis happened, but the task goal was not convincingly met due to missing primary metrics, incomplete validation, unclear novelty, or uncertain correctness.
- 0.20-0.35: poor / mostly failed. The attempt failed important gates, lacked reliable evidence, produced no meaningful improvement, was wrongly scoped, or was mostly duplicate.
- 0.00-0.20: failed / invalid / unsafe. No implementation, invalid hypothesis, duplicate with no new value, broken build, failed correctness gates, unsafe change, or unusable output.
- Reward clear execution evidence, relevant metrics, validation checks, diagnostics, and honest uncertainty. Reward artifacts that make the result or failure mode inspectable.
- Penalize unsupported claims, missing metrics, regressions, overfitting to feedback, duplicate work, or failure to follow the candidate contract.
- Evidence caps are mandatory upper bounds. Apply the lowest applicable cap before choosing the final score:
  - Cap at 0.75 if the primary metric was not freshly measured in this execution phase, uses historical logs/pre-existing files, or did not run the configured repeat count.
  - Cap at 0.70 if validation gates are incomplete, skipped, or only asserted by reasoning instead of command evidence.
  - Cap at 0.60 if the baseline is unclear, mismatched to the configured baseline, measured on a different setup, or the reported improvement may include accumulated prior-candidate changes rather than this candidate alone.
  - Cap at 0.50 if the trace contains contradictory evidence about metrics, validation, changed files, or whether the candidate was actually implemented.
  - Cap at 0.35 if there is no implementation commit/change when one was required, no primary metric, or no reliable validation evidence.
- Do not mark passed=true when an evidence cap was triggered by missing fresh metric repeats, incomplete validation, baseline mismatch, or suspected accumulated improvements; use confidence low or medium unless the uncertainty is resolved by trace evidence.
- Do not assume hidden task facts that are not present in candidate facts, trace evidence, the task goal, or the judger contract.
- Return actionable feedback that helps the next proposer.

Final delivery contract (mandatory):
- Your final response MUST be exactly one parseable JSON object matching the schema below.
- Wrapping the JSON in a ```json code fence is acceptable; any prose, Markdown table, heading, commentary, or natural-language summary outside the JSON is forbidden.
- NEVER return a judgement report in Markdown.
- If evidence is incomplete, ambiguous, contradictory, or insufficient, still return the JSON object with passed=false, an appropriately low score, failure_categories, actionable_feedback, and confidence="low|medium".
- Do not ask for more data, do not say you will continue later, and do not emit a natural-language wrap-up.

Required JSON schema:
{{
  "score": 0.0,
  "passed": false,
  "per_sample_scores": [{{"sample_id": "task_execution", "score": 0.0, "notes": ""}}],
  "failure_categories": ["category"],
  "actionable_feedback": ["specific next action"],
  "confidence": "low|medium|high"
}}
"""
        prompt = PromptAssembler().build_judge_prompt(candidate, trace, config, judger_context)
        repair_meta: dict[str, Any] = {}
        try:
            result = _run_agent_json(self.client, prompt, "judger", call_context)
        except AgentError as exc:
            raw_output = str(getattr(exc, "raw_output", None) or exc)[:4000]
            repair_retries = int(config.get("judger", {}).get("repair_retries", 1))
            if repair_retries <= 0:
                return self._fallback_judgment(candidate, config, raw_output, "judger returned non-JSON output")
            try:
                result = _run_agent_json(
                    self._repair_client_for_config(config),
                    self._repair_prompt(raw_output, judger_context),
                    "judger",
                    call_context,
                )
                validate_judgment_payload(result.data)
                repair_meta = {"repair_applied": True, "original_raw_output": raw_output}
            except (AgentError, AgentProtocolError) as repair_exc:
                repair_raw = str(getattr(repair_exc, "raw_output", None) or repair_exc)[:4000]
                return self._fallback_judgment(
                    candidate,
                    config,
                    raw_output,
                    "judger returned non-JSON output and repair failed",
                    repair_raw_output=repair_raw,
                )
        try:
            return self._judgment_from_data(candidate, config, result.data, result.text, result, repair_meta)
        except AgentProtocolError as exc:
            return self._fallback_judgment(
                candidate,
                config,
                str(getattr(result, "text", "") or result.data)[:4000],
                f"judger protocol invalid: {exc}",
                failure_category="judger_protocol_invalid",
            )

    def _repair_client_for_config(self, config: dict[str, Any]) -> ClaudeCodeClient:
        if not isinstance(self.client, ClaudeCodeClient):
            return self.client
        repair_timeout = int(config.get("judger", {}).get("repair_timeout_seconds", 300))
        timeout_seconds = min(int(self.client.timeout_seconds), repair_timeout)
        return ClaudeCodeClient(
            command=self.client.command,
            cwd=self.client.cwd,
            timeout_seconds=timeout_seconds,
            extra_args=list(self.client.extra_args),
            heartbeat_seconds=self.client.heartbeat_seconds,
            usage_tracker=self.client.usage_tracker,
        )

    def _repair_prompt(self, raw_output: str, judger_context: dict[str, Any]) -> str:
        return f"""
You are the JUDGER agent in a bounded GEPA-style research loop. A PREVIOUS attempt
at this exact judgment finished WITHOUT returning a parseable JSON object. Your ONLY
job now is to transcribe that judgment into the JSON schema below. DO NOT re-run,
re-score from scratch, inspect files, or ask for more data.

Previous non-JSON raw output:
{raw_output}

Judger context used for the previous attempt:
{format_judger_context(judger_context)}

Produce EXACTLY one JSON object matching this schema:
{{
  "score": 0.0,
  "passed": false,
  "per_sample_scores": [{{"sample_id": "task_execution", "score": 0.0, "notes": ""}}],
  "failure_categories": ["category"],
  "actionable_feedback": ["specific next action"],
  "confidence": "low|medium|high"
}}

If the previous raw output clearly contains a score, passed flag, sample scores,
failure categories, feedback, or confidence, preserve those facts. If any required
field is missing or ambiguous, choose the conservative value: score=0.0, passed=false,
failure_categories=["judger_invalid_json"], and confidence="low". Return only the
JSON object.
"""

    def _judgment_from_data(
        self,
        candidate: Candidate,
        config: dict[str, Any],
        data: dict[str, Any],
        raw_text: str,
        result: Any,
        extra_artifacts: dict[str, Any] | None = None,
    ) -> Judgment:
        data = validate_judgment_payload(data)
        try:
            score = float(data.get("score", 0.0))
        except (TypeError, ValueError):
            score = 0.0
        return Judgment(
            candidate_id=candidate.candidate_id,
            round_id=candidate.round_id,
            score=score,
            passed=bool(data.get("passed", False)),
            per_sample_scores=list(data.get("per_sample_scores", [])),
            failure_categories=list(data.get("failure_categories", [])),
            actionable_feedback=list(data.get("actionable_feedback", [])),
            confidence=str(data.get("confidence", "medium")),
            artifacts={
                "agent_raw": raw_text,
                "eval_phase": config.get("_eval_phase", "pareto"),
                "sample_ids": config.get("_selected_sample_ids", []),
                **_call_artifact(result),
                **data,
                **(extra_artifacts or {}),
            },
        )

    def _fallback_judgment(
        self,
        candidate: Candidate,
        config: dict[str, Any],
        raw_output: str,
        reason: str,
        *,
        repair_raw_output: str | None = None,
        failure_category: str = "judger_invalid_json",
    ) -> Judgment:
        artifacts = {
            "deterministic": True,
            "agent_raw": raw_output,
            "eval_phase": config.get("_eval_phase", "pareto"),
            "sample_ids": config.get("_selected_sample_ids", []),
            "error": reason,
        }
        if repair_raw_output is not None:
            artifacts["repair_raw_output"] = repair_raw_output
        return Judgment(
            candidate_id=candidate.candidate_id,
            round_id=candidate.round_id,
            score=0.0,
            passed=False,
            per_sample_scores=[],
            failure_categories=[failure_category],
            actionable_feedback=[
                "Judger returned non-JSON output; retry judging with the stricter JSON-only contract."
            ],
            confidence="low",
            artifacts=artifacts,
        )
