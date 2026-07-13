from __future__ import annotations

from datetime import datetime, timezone
import inspect
from pathlib import Path
from typing import Any

from .agent_client import AgentError, ClaudeCodeClient
from ..config.contracts import format_role_contract
from ..loop.context_views import (
    build_executor_context,
    build_judger_context,
    build_proposer_context,
    candidate_for_executor,
    evidence_access_policy,
)
from ..models.schemas import AgentCallContext, Candidate, CandidateBatch, Judgment, LoopState, SampleTrace, Trace


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


# Shared by the executor's normal and repair prompts so the two cannot drift.
_EXECUTOR_RESULT_SCHEMA = """{
  "summary": "what you executed",
  "implementation": {"changed_files": [], "commands_run": [], "notes": ""},
  "metrics": {"primary": null, "baseline": null, "delta": null},
  "validation": {"passed": false, "checks": [], "regressions": []},
  "diagnostics": ["diagnostic or failure finding"],
  "artifact_paths": ["relative or absolute paths"],
  "errors": []
}"""


class AgentProposer:
    def __init__(self, client: ClaudeCodeClient):
        self.client = client

    def propose(self, state: LoopState, config: dict[str, Any]) -> Candidate:
        proposer_context = build_proposer_context(state, config)
        prompt = f"""
You are the PROPOSER agent in a bounded GEPA-style research loop.

Task goal:
{config["task"]["goal"]}

{format_config_for_role(config, "proposer")}

{format_evidence_policy(config)}

{format_proposer_context(proposer_context)}

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
- Return only a JSON object, no prose outside JSON.

Required JSON schema:
{{
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
  "executor_contract": {{"instructions": "what the executor should attempt or verify", "expected_artifacts": ["artifact"], "success_criteria": ["criterion"]}},
  "expected_artifacts": ["artifact"],
  "mutation_note": "what prior feedback this candidate responds to"
}}
"""
        result = _run_agent_json(
            self.client,
            prompt,
            "proposer",
            AgentCallContext(
                role="proposer",
                round_id=state.round_id,
                phase=str(config.get("_agent_phase", "mutation")),
                candidate_ids=[f"cand_{state.round_id:03d}"],
            ),
        )
        data = result.data
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
            parent_ids=list((config.get("_gepa_context") or {}).get("pareto_frontier", {}).get("parent_ids", [])),
            executor_contract=dict(data.get("executor_contract", {})),
            expected_artifacts=list(data.get("expected_artifacts", [])),
            mutation_note=str(data.get("mutation_note", "")),
            target_files=list(map(str, data.get("target_files", []))),
            safety_class=str(data.get("safety_class", "")),
            strategy=str(data.get("strategy", "")),
            candidate_class=str(data.get("candidate_class", "")),
            expected_gain=_expected_gain(data),
            artifacts={"agent_raw": result.text, "eval_phase": config.get("_eval_phase", "pareto"), "sample_ids": config.get("_selected_sample_ids", []), **_call_artifact(result), **data},
        )

    def propose_batch(self, state: LoopState, config: dict[str, Any]) -> CandidateBatch:
        batch_size = int(config.get("generation", {}).get("batch_size", 10))
        proposer_context = build_proposer_context(state, config)
        prompt = f"""
You are the PROPOSER agent in a bounded GEPA-style research loop.

Task goal:
{config["task"]["goal"]}

{format_config_for_role(config, "proposer")}

{format_evidence_policy(config)}

{format_proposer_context(proposer_context)}

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
- Return only a JSON object, no prose outside JSON.

Required JSON schema:
{{
  "candidates": [
    {{
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
      "executor_contract": {{"instructions": "what the executor should attempt or verify", "expected_artifacts": ["artifact"], "success_criteria": ["criterion"]}},
      "expected_artifacts": ["artifact"],
      "mutation_note": "what prior feedback this candidate responds to"
    }}
  ]
}}
"""
        planned_ids = [f"cand_{state.round_id:03d}_{index:03d}" for index in range(batch_size)]
        result = _run_agent_json(
            self.client,
            prompt,
            "proposer",
            AgentCallContext(
                role="proposer",
                round_id=state.round_id,
                phase=str(config.get("_agent_phase", "mutation")),
                candidate_ids=planned_ids,
            ),
        )
        items = list(result.data.get("candidates", []))[:batch_size]
        parent_ids = list((config.get("_gepa_context") or {}).get("pareto_frontier", {}).get("parent_ids", []))
        candidates = []
        for index, data in enumerate(items):
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
                    artifacts={"agent_raw": result.text, "eval_phase": config.get("_eval_phase", "pareto"), "sample_ids": config.get("_selected_sample_ids", []), **_call_artifact(result), **data},
                )
            )
        return CandidateBatch(round_id=state.round_id, candidates=candidates)


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
        executor_context = build_executor_context(candidate, config, self.run_dir, visible_round_dir, visible_repo_dir, execution_mode)
        prompt = f"""
You are the EXECUTOR agent in a bounded GEPA-style research loop.

You may inspect files and run commands inside the repository. Your job is to
execute the proposed candidate under the configured task resources and return
structured evidence about what happened.

Task goal:
{config["task"]["goal"]}

{format_config_for_role(config, "executor")}

{format_evidence_policy(config)}

{format_executor_context(executor_context)}

{evidence_access_policy()}

Constraints:
- Do not ask the user for help.
- Do not assume hidden task facts that are not present in resources, prior context, or loop feedback.
- The user guarantees the provided paths in the config are sufficient to run the project; use project docs and reference commands as context.
- Reference commands are hints/context, not GEPA-enforced steps; choose commands by reading the project docs and repository.
- Keep this execution compact and scoped to the candidate contract.
- Avoid broad repository exploration unless the candidate contract requires it.
- You may inspect files, make bounded changes, run validation or benchmark commands,
  and compare against available baselines when useful for this candidate.
- Save any scripts or generated artifacts under the working directory above.
- In implement_and_validate mode, edit only admitted target_files and create no more than the configured commit budget.
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
- A partial or failed run is acceptable ONLY if it is reported as JSON.
- Set validation.passed=true only when all required validation and metric gates actually passed.

Required JSON schema:
{_EXECUTOR_RESULT_SCHEMA}
"""
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
        data = result.data
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
        judger_context = build_judger_context(candidate, trace, config)
        prompt = f"""
You are the JUDGER agent in a bounded GEPA-style research loop.

Evaluate whether the executor's result supports the candidate as a useful
improvement or valid finding for the configured task.

Task goal:
{config["task"]["goal"]}

{format_config_for_role(config, "judger")}

{format_judger_context(judger_context)}

{evidence_access_policy()}

Rubric:
- Score 0.0 to 1.0.
- Reward clear execution evidence, relevant metrics, validation checks, diagnostics, and honest uncertainty.
- Reward relevant artifacts that make the result or failure mode inspectable.
- Penalize missing evidence when the task naturally requires validation and validation was feasible.
- Penalize unsupported claims, missing metrics, regressions, overfitting to feedback, or failure to follow the candidate contract.
- Do not assume hidden task facts that are not present in candidate facts, trace evidence, the task goal, or the judger contract.
- Return actionable feedback that helps the next proposer.
- Return only a JSON object, no prose outside JSON.

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
        result = _run_agent_json(
            self.client,
            prompt,
            "judger",
            AgentCallContext(
                role="judger",
                round_id=candidate.round_id,
                phase=str(config.get("_eval_phase", "pareto")),
                candidate_id=candidate.candidate_id,
                execution_id=config.get("_execution_id"),
                parent_candidate_id=candidate.parent_ids[0] if candidate.parent_ids else None,
            ),
        )
        data = result.data
        return Judgment(
            candidate_id=candidate.candidate_id,
            round_id=candidate.round_id,
            score=float(data.get("score", 0.0)),
            passed=bool(data.get("passed", False)),
            per_sample_scores=list(data.get("per_sample_scores", [])),
            failure_categories=list(data.get("failure_categories", [])),
            actionable_feedback=list(data.get("actionable_feedback", [])),
            confidence=str(data.get("confidence", "medium")),
            artifacts={"agent_raw": result.text, "eval_phase": config.get("_eval_phase", "pareto"), "sample_ids": config.get("_selected_sample_ids", []), **_call_artifact(result), **data},
        )


