from __future__ import annotations

from datetime import datetime, timezone
import inspect
from pathlib import Path
from typing import Any

from .agent_client import ClaudeCodeClient
from .context_views import candidate_for_agent, evidence_access_policy, state_for_agent, trace_for_agent
from .schemas import AgentCallContext, Candidate, CandidateBatch, Decision, Judgment, LoopState, SampleTrace, Trace


def format_runtime(config: dict[str, Any]) -> str:
    runtime = config.get("runtime", {})
    if not runtime:
        return "Runtime environment:\n- Not specified; use only commands explicitly allowed by the run configuration."

    lines = ["Runtime environment:"]
    environment = runtime.get("environment")
    conda_env = runtime.get("conda_env")
    python_command = runtime.get("python_command")
    dependency_policy = runtime.get("dependency_policy")
    allowed_commands = runtime.get("allowed_commands", [])

    if environment:
        lines.append(f"- Environment type: {environment}")
    if conda_env:
        lines.append(f"- Conda environment: {conda_env}")
    if python_command:
        lines.append(f"- Python command: {python_command}")
    if dependency_policy:
        lines.append(f"- Dependency policy: {dependency_policy}")
    if allowed_commands:
        lines.append(f"- Allowed shell commands: {allowed_commands}")
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


def format_prior_context(config: dict[str, Any]) -> str:
    context = config.get("_prior_context") or {}
    if not context:
        return "Prior context:\n- No prior context loaded."
    return (
        "Prior context:\n"
        f"- Notes: {context.get('notes', [])}\n"
        f"- Skills: {context.get('skills', [])}\n"
        f"- Documents: {context.get('documents', [])}\n"
        f"- Warnings: {context.get('warnings', [])}"
    )


def format_gepa_context(config: dict[str, Any]) -> str:
    context = config.get("_gepa_context")
    if not context:
        return "GEPA context:\n- No prior candidate pool exists yet; create seed candidate(s)."
    return (
        "GEPA context:\n"
        f"- Pareto frontier: {context.get('pareto_frontier', {})}\n"
        f"- Parent candidates: {context.get('parents', [])}\n"
        f"- Parent trace artifacts: {context.get('parent_traces', {})}\n"
        f"- Score matrix: {context.get('score_matrix', {})}\n"
        f"- Recent feedback: {context.get('recent_feedback', [])}\n"
        f"- Recent traces: {context.get('recent_traces', [])}\n"
        f"- Dataset split: {context.get('dataset_split', {})}"
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
    strategies = policy.get("allowed_strategies", [])
    if strategies:
        lines.append(f"- strategy must begin with one of: {strategies}")
    safety = policy.get("allowed_safety_classes", [])
    if safety:
        lines.append(f"- safety_class must be one of: {safety}")
    classes = policy.get("allowed_candidate_classes", [])
    if classes:
        lines.append(f"- candidate_class must be one of: {classes}")
    max_targets = policy.get("max_target_files")
    if max_targets is not None:
        lines.append(f"- max_target_files: {max_targets}")
    return "\n".join(lines)


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
    value = data.get("expected_gain")
    if value is None:
        value = data.get("expected_gain_ms_evt")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _call_artifact(result) -> dict[str, Any]:
    record = getattr(result, "call_record", None)
    return {"agent_call_id": record.call_id} if record is not None else {}


class AgentProposer:
    def __init__(self, client: ClaudeCodeClient):
        self.client = client

    def propose(self, state: LoopState, config: dict[str, Any]) -> Candidate:
        prompt = f"""
You are the PROPOSER agent in a bounded GEPA-style research loop.

Task goal:
{config["task"]["goal"]}

{format_task_resources(config)}

{format_candidate_policy(config)}

{format_runtime(config)}

{format_evidence_policy(config)}

{format_prior_context(config)}

{format_gepa_context(config)}

{evidence_access_policy()}

Current state facts:
{state_for_agent(state)}

Important constraints:
- Propose exactly one candidate research hypothesis or intervention for the next round.
- If parent candidates are provided, mutate from them instead of starting from scratch.
- Include an executor_contract that tells the external executor what to run and what to return.
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
  "safety_class": "task-defined safety class",
  "candidate_class": "safe-source|exploratory-source|build-tuning|algorithmic|external-compute",
  "expected_gain": 0.0,
  "analysis_plan": ["step 1", "step 2"],
  "executor_contract": {{"instructions": "what the executor must do", "expected_artifacts": ["artifact"], "success_criteria": ["criterion"]}},
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
            parent_id=state.best_candidate_id,
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
            expected_gain=_expected_gain(data),
            artifacts={"agent_raw": result.text, "eval_phase": config.get("_eval_phase", "pareto"), "sample_ids": config.get("_selected_sample_ids", []), **_call_artifact(result), **data},
        )

    def propose_batch(self, state: LoopState, config: dict[str, Any]) -> CandidateBatch:
        batch_size = int(config.get("generation", {}).get("batch_size", 10))
        prompt = f"""
You are the PROPOSER agent in a bounded GEPA-style research loop.

Task goal:
{config["task"]["goal"]}

{format_task_resources(config)}

{format_candidate_policy(config)}

{format_runtime(config)}

{format_evidence_policy(config)}

{format_prior_context(config)}

{format_gepa_context(config)}

{evidence_access_policy()}

Current state facts:
{state_for_agent(state)}

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
      "safety_class": "task-defined safety class",
      "candidate_class": "safe-source|exploratory-source|build-tuning|algorithmic|external-compute",
      "expected_gain": 0.0,
      "analysis_plan": ["step 1", "step 2"],
      "executor_contract": {{"instructions": "what the executor must do", "expected_artifacts": ["artifact"], "success_criteria": ["criterion"]}},
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
                    parent_id=state.best_candidate_id,
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
        round_dir = Path(
            config.get("_candidate_workspace")
            or self.run_dir / "agent_work" / f"round_{candidate.round_id:03d}" / candidate.candidate_id
        )
        round_dir.mkdir(parents=True, exist_ok=True)
        repo_dir = Path(config.get("_candidate_repo") or getattr(self.client, "cwd", None) or round_dir)
        execution_mode = str(config.get("_execution_mode", "implement_and_validate"))
        prompt = f"""
You are the EXECUTOR agent in a bounded GEPA-style research loop.

You may inspect files and run commands inside the repository. Your job is to
execute the proposed candidate under the configured task resources and return
structured evidence about what happened.

Task goal:
{config["task"]["goal"]}

{format_task_resources(config)}

{format_candidate_policy(config)}

{format_runtime(config)}

{format_evidence_policy(config)}

{format_prior_context(config)}

Evaluation phase: {config.get("_eval_phase", "pareto")}
Execution mode: {execution_mode}
Selected sample ids: {config.get("_selected_sample_ids", [])}

Candidate decision facts:
{candidate_for_agent(candidate, [str(self.run_dir / "traces" / f"round_{candidate.round_id:03d}" / candidate.candidate_id / "candidate.json")])}

{evidence_access_policy()}

Working directory for any scripts/artifacts you create:
{round_dir}

Candidate source repository:
{repo_dir}

Constraints:
- Do not ask the user for help.
- Do not assume hidden task facts that are not present in resources, prior context, or loop feedback.
- Use the configured Python command from the runtime environment above for any Python execution.
- Keep this execution compact and scoped to the candidate contract.
- Avoid broad repository exploration unless the candidate contract requires it.
- You may inspect files, make bounded changes, run validation or benchmark commands,
  and compare against available baselines when useful for this candidate and allowed
  by the runtime policy.
- Save any scripts or generated artifacts under the working directory above.
- In implement_and_validate mode, edit only admitted target_files and create no more than the configured commit budget.
- In evaluate_only mode, do not edit source files, create commits, switch branches, or change HEAD.
- Never run git checkout, git switch, or git worktree; the orchestrator owns Git lifecycle.
- When visual evidence is feasible, follow the candidate's visual evidence plan.
  Save plot file(s) under the working directory and list them in artifact_paths.
- Return only a JSON object, no prose outside JSON.

Required JSON schema:
{{
  "summary": "what you executed",
  "implementation": {{"changed_files": [], "commands_run": [], "notes": ""}},
  "metrics": {{"primary": null, "baseline": null, "delta": null}},
  "validation": {{"passed": false, "checks": [], "regressions": []}},
  "diagnostics": ["diagnostic or failure finding"],
  "artifact_paths": ["relative or absolute paths"],
  "errors": []
}}
"""
        client = self._client_for_config(config)
        result = _run_agent_json(
            client,
            prompt,
            "executor",
            AgentCallContext(
                role="executor",
                round_id=candidate.round_id,
                phase=str(config.get("_eval_phase", "pareto")),
                candidate_id=candidate.candidate_id,
                execution_id=config.get("_execution_id"),
                parent_candidate_id=candidate.parent_id,
            ),
            cwd=repo_dir,
            env=dict(config.get("_candidate_env") or {}),
        )
        data = result.data
        trace = SampleTrace(
            sample_id=str((config.get("_selected_sample_ids") or ["task_execution"])[0]),
            input=str(config.get("task", {})),
            output=str(data),
            expected="unknown",
            logs=str(data.get("summary", "")),
            error="; ".join(data.get("errors", [])) if data.get("errors") else None,
            artifacts={"agent_raw": result.text, "eval_phase": config.get("_eval_phase", "pareto"), "sample_ids": config.get("_selected_sample_ids", []), "execution_mode": execution_mode, **_call_artifact(result), **data},
        )
        return Trace(candidate_id=candidate.candidate_id, round_id=candidate.round_id, samples=[trace])

    def _client_for_config(self, config: dict[str, Any]) -> ClaudeCodeClient:
        timeout = config.get("_executor_timeout_seconds")
        if timeout is None:
            return self.client
        return ClaudeCodeClient(
            command=self.client.command,
            cwd=self.client.cwd,
            timeout_seconds=int(timeout),
            extra_args=list(self.client.extra_args),
            heartbeat_seconds=self.client.heartbeat_seconds,
            usage_tracker=self.client.usage_tracker,
        )


class AgentJudger:
    def __init__(self, client: ClaudeCodeClient):
        self.client = client

    def judge(self, candidate: Candidate, trace: Trace, config: dict[str, Any]) -> Judgment:
        prompt = f"""
You are the JUDGER agent in a bounded GEPA-style research loop.

Evaluate whether the executor's result supports the candidate as a useful
improvement or valid finding for the configured task.

Task goal:
{config["task"]["goal"]}

Candidate decision facts:
{candidate_for_agent(candidate, [str(Path(config.get("_run_dir", ".")) / "traces" / f"round_{candidate.round_id:03d}" / candidate.candidate_id / "candidate.json")])}

Trace decision facts:
{trace_for_agent(trace, [str(Path(config.get("_run_dir", ".")) / "traces" / f"round_{trace.round_id:03d}" / trace.candidate_id / "trace.json")])}

{format_evidence_policy(config)}

{format_prior_context(config)}

{evidence_access_policy()}

Evaluation phase: {config.get("_eval_phase", "pareto")}
Selected sample ids: {config.get("_selected_sample_ids", [])}

Rubric:
- Score 0.0 to 1.0.
- Reward clear execution evidence, relevant metrics, validation checks, diagnostics, and honest uncertainty.
- Reward relevant artifacts that make the result or failure mode inspectable.
- Penalize missing evidence when the task naturally requires validation and validation was feasible.
- Penalize unsupported claims, missing metrics, regressions, overfitting to feedback, or failure to follow the candidate contract.
- Do not assume hidden task facts that are not present in resources, prior context, or loop feedback.
- Return actionable feedback that helps the next proposer.
- Return only a JSON object, no prose outside JSON.

Required JSON schema:
{{
  "score": 0.0,
  "passed": false,
  "per_sample_scores": [{{"sample_id": "task_execution", "score": 0.0, "notes": ""}}],
  "failure_categories": ["category"],
  "actionable_feedback": ["specific next action"],
  "confidence": "low|medium|high",
  "best_interpretation": "brief interpretation"
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
                parent_candidate_id=candidate.parent_id,
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


class AgentGater:
    def __init__(self, client: ClaudeCodeClient):
        self.client = client

    def decide(self, state: LoopState, candidate: Candidate, judgment: Judgment, config: dict[str, Any]) -> Decision:
        prompt = f"""
You are the GATER agent in a bounded GEPA-style research loop.

You decide whether to keep, reject, iterate, or stop. You must respect the
budget and cannot change scores.

Budget:
{config["budget"]}

State JSON:
{state.to_dict()}

Candidate JSON:
{candidate.to_dict()}

Judgment JSON:
{judgment.to_dict()}

Rules:
- Stop if pass_threshold is reached.
- Stop if this is the final allowed round.
- Prefer keep/iterate when there is useful feedback and budget remains.
- Reject candidates that do not improve and have no useful path forward.
- Return only a JSON object, no prose outside JSON.

Required JSON schema:
{{
  "decision": "keep|reject|iterate|stop",
  "reason": "why",
  "best_so_far": "candidate id or null",
  "stop": false,
  "next_focus": "what the next proposer should focus on"
}}
"""
        result = _run_agent_json(
            self.client,
            prompt,
            "gater",
            AgentCallContext(
                role="gater",
                round_id=candidate.round_id,
                phase="gate",
                candidate_id=candidate.candidate_id,
                parent_candidate_id=candidate.parent_id,
            ),
        )
        data = result.data
        decision = str(data.get("decision", "reject"))
        if decision not in {"keep", "reject", "iterate", "stop"}:
            decision = "reject"

        # Hard safety overlay: never let the agent exceed explicit loop budget.
        pass_threshold = float(config["judger"].get("pass_threshold", 1.0))
        max_rounds = int(config["budget"]["max_rounds"])
        stop = bool(data.get("stop", False))
        if judgment.score >= pass_threshold or state.round_id + 1 >= max_rounds:
            stop = True
            decision = "stop"

        return Decision(
            candidate_id=candidate.candidate_id,
            round_id=candidate.round_id,
            decision=decision,  # type: ignore[arg-type]
            reason=str(data.get("reason", "")),
            best_so_far=data.get("best_so_far") or state.best_candidate_id or candidate.candidate_id,
            stop=stop,
            artifacts={"agent_raw": result.text, "eval_phase": config.get("_eval_phase", "pareto"), "sample_ids": config.get("_selected_sample_ids", []), **data},
        )
