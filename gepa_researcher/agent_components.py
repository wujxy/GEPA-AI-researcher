from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .agent_client import ClaudeCodeClient
from .schemas import Candidate, Decision, Judgment, LoopState, SampleTrace, Trace


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


class AgentProposer:
    def __init__(self, client: ClaudeCodeClient):
        self.client = client

    def propose(self, state: LoopState, config: dict[str, Any]) -> Candidate:
        prompt = f"""
You are the PROPOSER agent in a bounded GEPA-style research loop.

Task goal:
{config["task"]["goal"]}

Available data files:
{config["task"].get("data_files", [])}

{format_runtime(config)}

{format_evidence_policy(config)}

Current state JSON:
{state.to_dict()}

Important constraints:
- Propose exactly one candidate research hypothesis/model for the next round.
- Do not assume any hidden data-generating process.
- Use only the observed data files and prior loop feedback.
- Keep the candidate small enough for the executor to test in one round.
- Propose candidates that are executable in the runtime environment above.
- Include visual diagnostics in the analysis plan when they can support or falsify the model.
- Choose the plot type(s) that best fit this specific task and candidate; do not rely on a fixed plot template.
- Return only a JSON object, no prose outside JSON.

Required JSON schema:
{{
  "hypothesis": "short falsifiable model hypothesis",
  "target_module": "distribution_model",
  "proposed_change": "what to test this round",
  "rationale": "why this is a good next candidate",
  "expected_improvement": "what metric should improve",
  "risk": "main risk or failure mode",
  "model_family": "e.g. normal, lognormal, mixture, uniform, exponential, nonparametric",
  "analysis_plan": ["step 1", "step 2"]
}}
"""
        result = self.client.run_json(prompt, label="proposer")
        data = result.data
        candidate_id = f"cand_{state.round_id:03d}"
        model_family = str(data.get("model_family", "unspecified"))
        prompt_text = (
            f"Model family: {model_family}\n"
            f"Hypothesis: {data.get('hypothesis', '')}\n"
            f"Plan: {data.get('analysis_plan', [])}"
        )
        return Candidate(
            candidate_id=candidate_id,
            round_id=state.round_id,
            parent_id=state.best_candidate_id,
            hypothesis=str(data.get("hypothesis", "")),
            target_module=str(data.get("target_module", "distribution_model")),
            proposed_change=str(data.get("proposed_change", "")),
            rationale=str(data.get("rationale", "")),
            expected_improvement=str(data.get("expected_improvement", "")),
            risk=str(data.get("risk", "")),
            prompt_text=prompt_text,
            created_at=datetime.now(timezone.utc).isoformat(),
            artifacts={"agent_raw": result.text, **data},
        )


class AgentExecutor:
    def __init__(self, client: ClaudeCodeClient, run_dir: Path):
        self.client = client
        self.run_dir = run_dir

    def execute(self, candidate: Candidate, config: dict[str, Any]) -> Trace:
        round_dir = self.run_dir / "agent_work" / f"round_{candidate.round_id:03d}"
        round_dir.mkdir(parents=True, exist_ok=True)
        prompt = f"""
You are the EXECUTOR agent in a bounded GEPA-style research loop.

You may inspect files and run commands inside the repository. Your job is to
test the proposed candidate model on the observed numeric data.

Task goal:
{config["task"]["goal"]}

Data files:
{config["task"].get("data_files", [])}

{format_runtime(config)}

{format_evidence_policy(config)}

Candidate JSON:
{candidate.to_dict()}

Working directory for any scripts/artifacts you create:
{round_dir}

Constraints:
- Do not ask the user for help.
- Do not assume any hidden data-generating process.
- Use the configured Python command from the runtime environment above for any Python execution.
- Keep this execution compact. Prefer this sequence: inspect the data file once,
  write at most one small Python script if needed, run it once, then return JSON.
- Avoid broad repository exploration; the candidate and data files are the scope.
- You may fit parameters, compute descriptive statistics, likelihood/AIC/BIC,
  goodness-of-fit diagnostics, residual summaries, and compare simple baselines
  only if useful for this candidate.
- Save any scripts or generated artifacts under the working directory above.
- When visual evidence is feasible, follow the candidate's visual evidence plan.
  Save plot file(s) under the working directory and list them in artifact_paths.
- Return only a JSON object, no prose outside JSON.

Required JSON schema:
{{
  "summary": "what you executed",
  "model_expression": "mathematical/statistical expression tested",
  "fit_parameters": {{}},
  "metrics": {{}},
  "diagnostics": ["diagnostic finding"],
  "artifact_paths": ["relative or absolute paths"],
  "errors": []
}}
"""
        result = self.client.run_json(prompt, label="executor")
        data = result.data
        trace = SampleTrace(
            sample_id="observed_numeric_dataset",
            input=str(config["task"].get("data_files", [])),
            output=str(data),
            expected="unknown",
            logs=str(data.get("summary", "")),
            error="; ".join(data.get("errors", [])) if data.get("errors") else None,
            artifacts={"agent_raw": result.text, **data},
        )
        return Trace(candidate_id=candidate.candidate_id, round_id=candidate.round_id, samples=[trace])


class AgentJudger:
    def __init__(self, client: ClaudeCodeClient):
        self.client = client

    def judge(self, candidate: Candidate, trace: Trace, config: dict[str, Any]) -> Judgment:
        prompt = f"""
You are the JUDGER agent in a bounded GEPA-style research loop.

Evaluate whether the executor's result supports the candidate model as a useful
description of the observed numeric dataset.

Task goal:
{config["task"]["goal"]}

Candidate JSON:
{candidate.to_dict()}

Trace JSON:
{trace.to_dict()}

{format_evidence_policy(config)}

Rubric:
- Score 0.0 to 1.0.
- Reward clear quantitative fit, parameter estimates, diagnostics, and honest uncertainty.
- Reward relevant visual artifacts that make the fit or failure mode inspectable.
- Penalize missing visual evidence when the task is naturally plottable and plotting was feasible.
- Penalize unsupported claims, missing metrics, overfitting, or failure to inspect the data.
- Do not assume any hidden data-generating process.
- Return actionable feedback that helps the next proposer.
- Return only a JSON object, no prose outside JSON.

Required JSON schema:
{{
  "score": 0.0,
  "passed": false,
  "per_sample_scores": [{{"sample_id": "observed_numeric_dataset", "score": 0.0, "notes": ""}}],
  "failure_categories": ["category"],
  "actionable_feedback": ["specific next action"],
  "confidence": "low|medium|high",
  "best_interpretation": "brief interpretation"
}}
"""
        result = self.client.run_json(prompt, label="judger")
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
            artifacts={"agent_raw": result.text, **data},
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
        result = self.client.run_json(prompt, label="gater")
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
            artifacts={"agent_raw": result.text, **data},
        )
