from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..config.contracts import format_role_contract
from ..models.schemas import Candidate, LoopState, Trace
from .blocks import ContextBlock, ContextBlockKind, ContextRenderMode, ContextRole
from .views import ContextView


@dataclass(frozen=True)
class PromptSection:
    title: str
    body: str
    mandatory: bool = True


_PROPOSER_SCHEMA = '''{
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
}'''

_PROPOSER_FINAL_CONTRACT = '''Final delivery contract (mandatory):
- Your final response MUST be exactly one parseable JSON object matching the required schema below.
- Wrapping the JSON in a ```json code fence is acceptable; any prose, Markdown table, heading, commentary, status update, or natural-language wrap-up outside the JSON is forbidden.
- NEVER say that you have already submitted candidates, that you will continue later, or that another agent completed work unless that text is inside a JSON string field.
- If you are uncertain, blocked, or found that an idea is infeasible, still return the required JSON object with conservative fields, explicit risk, and an analysis_plan step that verifies the uncertainty.
- Do not ask for more data and do not emit a natural-language summary.'''

_EXECUTOR_SCHEMA = '''{
  "summary": "what you executed",
  "implementation": {"changed_files": [], "commands_run": [], "commit_sha": null, "committed_files": [], "git_status_after_commit": "", "notes": ""},
  "metrics": {"primary": null, "baseline": null, "delta": null},
  "validation": {"passed": false, "checks": [], "regressions": []},
  "diagnostics": ["diagnostic or failure finding"],
  "artifact_paths": ["relative or absolute paths"],
  "errors": []
}'''


class PromptAssembler:
    """Render role-scoped context into stable agent prompts."""

    def __init__(self, max_prompt_blocks: int | None = None):
        self.max_prompt_blocks = max_prompt_blocks

    def build_proposer_prompt(
        self,
        state: LoopState,
        config: dict[str, Any],
        view: ContextView | dict[str, Any],
        batch_size: int | None = None,
    ) -> str:
        count = batch_size or 1
        proposal_count = (
            f"exactly {count} candidate research hypotheses or interventions for the next generation"
            if batch_size is not None
            else "exactly one candidate research hypothesis or intervention for the next round"
        )
        mutation = (
            "- If parent candidates are provided, each proposal must be a reflective mutation of the Pareto frontier parent(s).\n"
            "- Make the candidates meaningfully diverse while staying grounded in parent feedback.\n"
            "- Include executor_contract and expected_artifacts for every candidate."
            if batch_size is not None
            else "- If parent candidates are provided, mutate from them instead of starting from scratch.\n"
            "- Include an executor_contract that suggests what the executor should attempt and what evidence to return."
        )
        schema = _PROPOSER_SCHEMA if batch_size is None else '{\n  "candidates": [\n    ' + _PROPOSER_SCHEMA + '\n  ]\n}'
        return self._join(
            PromptSection("role", "You are the PROPOSER agent in a bounded GEPA-style research loop."),
            PromptSection("task", f"Task goal:\n{config['task']['goal']}"),
            PromptSection("contract", _format_config_for_role(config, "proposer")),
            PromptSection("evidence", _format_evidence_policy(config)),
            PromptSection("context", self.render_context(view, "proposer", empty="Proposer role context:\n- No prior candidate pool exists yet; create seed candidate(s).", max_prompt_blocks=_config_prompt_budget(config))),
            PromptSection("evidence-policy", _evidence_access_policy()),
            PromptSection("constraints", f'''Important constraints:
- Propose {proposal_count}.
{mutation}
- Do not assume hidden task facts that are not present in resources, prior context, or loop feedback.
- Use only the configured resources and prior loop feedback.
- Keep {"each candidate" if batch_size is not None else "the candidate"} small enough for the executor to test in one {"isolated workspace" if batch_size is not None else "round"}.
- Propose candidates that are executable in the runtime environment above.
- Include diagnostics or evidence artifacts in {"each" if batch_size is not None else "the"} analysis plan when they can support or falsify the candidate.
{"" if batch_size is not None else "- Choose task-appropriate evidence; do not rely on a fixed artifact template."}
{_PROPOSER_FINAL_CONTRACT}

Required JSON schema:
{schema}'''),
        )

    def build_executor_prompt(self, candidate: Candidate, config: dict[str, Any], view: ContextView | dict[str, Any]) -> str:
        return self._join(
            PromptSection("role", '''You are the EXECUTOR agent in a bounded GEPA-style research loop.

You may inspect files and run commands inside the repository. Your job is to
execute the proposed candidate under the configured task resources and return
structured evidence about what happened.'''),
            PromptSection("task", f"Task goal:\n{config['task']['goal']}"),
            PromptSection("contract", _format_config_for_role(config, "executor")),
            PromptSection("evidence", _format_evidence_policy(config)),
            PromptSection("context", self.render_context(view, "executor", max_prompt_blocks=_config_prompt_budget(config))),
            PromptSection("evidence-policy", _evidence_access_policy()),
            PromptSection("constraints", f'''Constraints:
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
- You may inspect files, make bounded changes, run validation or benchmark commands, and compare against available baselines when useful for this candidate.
- Save any scripts or generated artifacts under the working directory above.
- In implement_and_validate mode, edit only admitted target_files.
- In implement_and_validate mode, you MUST create a Git commit for the candidate source changes before your final JSON response. The orchestrator reads HEAD as the candidate result revision; uncommitted edits are treated as no implementation.
- Before committing, run git status --porcelain and git diff --name-only. Stage only admitted target_files with git add -- <target_files>; do not stage build outputs, benchmark logs, fixtures, caches, or other runtime artifacts.
- Run git commit with a concise candidate-scoped message, then run git rev-parse HEAD and put that SHA in implementation.commit_sha. Also report implementation.committed_files and implementation.git_status_after_commit.
- If you cannot create the commit, set validation.passed=false, leave implementation.commit_sha=null, and explain the exact git/status failure in errors and diagnostics.
- In evaluate_only mode, do not edit source files, create commits, switch branches, or change HEAD.
- Never run git checkout, git switch, or git worktree; the orchestrator owns Git lifecycle.
- When visual evidence is feasible, follow the candidate's visual evidence plan. Save plot file(s) under the working directory and list them in artifact_paths.

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
{_EXECUTOR_SCHEMA}'''),
        )

    def build_judge_prompt(self, candidate: Candidate, trace: Trace, config: dict[str, Any], view: ContextView | dict[str, Any]) -> str:
        return self._join(
            PromptSection("role", "You are the JUDGER agent in a bounded GEPA-style research loop."),
            PromptSection("purpose", "Evaluate whether the executor's result supports the candidate as a useful\nimprovement or valid finding for the configured task."),
            PromptSection("task", f"Task goal:\n{config['task']['goal']}"),
            PromptSection("contract", _format_config_for_role(config, "judger")),
            PromptSection("context", self.render_context(view, "judger", max_prompt_blocks=_config_prompt_budget(config))),
            PromptSection("evidence-policy", _evidence_access_policy()),
            PromptSection("rubric", _JUDGE_RUBRIC),
            PromptSection("delivery", _JUDGE_DELIVERY),
        )

    def render_context(
        self,
        view: ContextView | dict[str, Any],
        role: str,
        *,
        empty: str = "",
        max_prompt_blocks: int | None = None,
    ) -> str:
        if isinstance(view, ContextView):
            return self.render_context_blocks(view, max_prompt_blocks=max_prompt_blocks)
        return _render_legacy_context(view, role, empty)

    def render_context_blocks(self, view: ContextView, *, max_prompt_blocks: int | None = None) -> str:
        lines = ["Context envelope:", json.dumps(view.envelope.to_dict(), ensure_ascii=False, sort_keys=True)]
        selected, omitted = self._select_blocks(view, max_prompt_blocks)
        for block in selected:
            lines.append(self._render_block(block))
        if omitted:
            lines.extend(["omitted_context_refs:", json.dumps(omitted, ensure_ascii=False)])
        if view.metadata:
            lines.extend(["Context metadata:", json.dumps(view.metadata, ensure_ascii=False, sort_keys=True)])
        return "\n\n".join(lines)

    def _select_blocks(
        self, view: ContextView, max_prompt_blocks: int | None
    ) -> tuple[list[ContextBlock], list[str]]:
        blocks = sorted(view.blocks, key=lambda item: (item.kind.value, item.block_id))
        budget = self.max_prompt_blocks if max_prompt_blocks is None else max_prompt_blocks
        if budget is None:
            return blocks, []

        mandatory_kinds = {ContextBlockKind.RUN_FACT, ContextBlockKind.LOOP_STATE}
        if view.role in (ContextRole.EXECUTOR, ContextRole.JUDGE):
            mandatory_kinds.add(ContextBlockKind.CANDIDATE_FACT)
        mandatory = [block for block in blocks if block.kind in mandatory_kinds]
        optional = [block for block in blocks if block.kind not in mandatory_kinds]
        selected = mandatory + optional[:max(0, budget - len(mandatory))]
        selected_ids = {block.block_id for block in selected}
        omitted = [block.block_id for block in blocks if block.block_id not in selected_ids]
        return selected, omitted

    def _render_block(self, block: ContextBlock) -> str:
        sources = ", ".join(
            f"{ref.source_type}:{ref.source_id}" if ref.source_id else ref.source_type
            for ref in block.source_refs
        )
        header = f"{block.title} [kind={block.kind.value}; sources=[{sources}]]"
        if block.render_mode is ContextRenderMode.REF or block.kind is ContextBlockKind.ARTIFACT_REF:
            return f"{header}: ref={block.block_id}"
        if block.render_mode is ContextRenderMode.SUMMARY or block.inline_content is None:
            return f"{header}: {block.summary or ''}\ncontent_ref={block.block_id}"
        content = json.dumps(block.inline_content, ensure_ascii=False, sort_keys=True, default=str)
        return f"{header}: {block.summary or ''}\n{content}"

    @staticmethod
    def _join(*sections: PromptSection) -> str:
        return "\n\n".join(section.body.strip() for section in sections if section.mandatory or section.body).strip() + "\n"


def _format_config_for_role(config: dict[str, Any], role: str) -> str:
    contract = format_role_contract(config, role)
    if contract:
        return contract
    if role == "judger":
        return ""
    return "\n\n".join([_format_task_resources(config), _format_candidate_policy(config), _format_runtime(config)])


def _config_prompt_budget(config: dict[str, Any]) -> int | None:
    context_config = config.get("context", {})
    return context_config.get("max_prompt_blocks")


def _format_runtime(config: dict[str, Any]) -> str:
    runtime = config.get("runtime", {})
    if not runtime:
        return "Runtime envelope:\n- Not specified; inspect the project and available resources."
    labels = (("environment", "Environment type"), ("conda_env", "Conda environment"), ("python_command", "Python command"), ("dependency_policy", "Dependency policy"), ("guarantee", "Guarantee"))
    lines = ["Runtime environment:"]
    for key, label in labels:
        if runtime.get(key):
            lines.append(f"- {label}: {runtime[key]}")
    if runtime.get("allowed_commands"):
        lines.append(f"- Reference commands from legacy config (not mandatory): {runtime['allowed_commands']}")
    lines.extend(["- Reference commands and environment notes are hints/context, not GEPA-enforced steps.", "- Do not install new packages during the loop.", "- If a package is unavailable, record the import error and fall back to a simpler available method."])
    return "\n".join(lines)


def _format_evidence_policy(config: dict[str, Any]) -> str:
    evidence = config.get("evidence", {})
    if not evidence:
        return "Visual evidence:\n- No explicit visual evidence policy configured."
    lines = ["Visual evidence:"]
    if evidence.get("visualize_when_applicable", False):
        lines.append("- When the task can be explained or validated visually, create plot artifacts whenever feasible.")
    if evidence.get("plot_selection_policy") == "proposer_selects":
        lines.append("- The proposer should choose task-appropriate plots; do not assume a fixed plot set for every task.")
    if evidence.get("artifact_formats"):
        lines.append(f"- Preferred artifact formats: {evidence['artifact_formats']}")
    if evidence.get("guidance"):
        lines.append(f"- Guidance: {evidence['guidance']}")
    lines.extend(["- Save visual artifacts under the provided working directory and include their paths in artifact_paths.", "- If plotting is not possible in the runtime, explain why in errors or diagnostics."])
    return "\n".join(lines)


def _format_task_resources(config: dict[str, Any]) -> str:
    task = config.get("task", {})
    fields = {key: task.get(key, []) for key in ("data_files", "repo_paths", "workspaces", "benchmark_commands", "validation_commands", "artifacts") if task.get(key)}
    if not fields:
        return "Task resources:\n- No task resources configured."
    return "\n".join(["Task resources:", *(f"- {key}: {value}" for key, value in fields.items())])


def _format_candidate_policy(config: dict[str, Any]) -> str:
    policy = config.get("candidate_policy", {})
    if not policy:
        return "Candidate policy:\n- No deterministic admission policy configured."
    lines = ["Candidate policy:"]
    if config.get("workspace", {}).get("baseline_ref"):
        lines.append(f"- Source baseline/ref: {config['workspace']['baseline_ref']}")
    if policy.get("known_target_files"):
        lines.append("- target_files must be copied exactly from this known source list when applicable:")
        lines.extend(f"  - {path}" for path in policy["known_target_files"])
    for key in ("allowed_target_globs", "frozen_globs", "max_target_files"):
        if policy.get(key) is not None and policy.get(key) != []:
            lines.append(f"- {key}: {policy[key]}")
    return "\n".join(lines)


def _evidence_access_policy() -> str:
    return "Context evidence policy:\n- Use structured facts and metrics as the default evidence.\n- Only read evidence_refs when the structured context is insufficient, ambiguous, or contradictory.\n- If you read an evidence_ref, use it to resolve the specific missing fact and keep your response compact."


def _render_legacy_context(context: dict[str, Any], role: str, empty: str) -> str:
    if role == "proposer":
        if not context:
            return empty
        prior = context.get("prior_context") or {}
        return f"""Prior context:
- Notes: {prior.get('notes', [])}
- Skills: {prior.get('skills', [])}
- Documents: {prior.get('documents', [])}
- Warnings: {prior.get('warnings', [])}

Proposer role context:
- Frontier: {context.get('frontier', {})}
- Parent candidates: {context.get('parents', [])}
- Score summary: {context.get('score_summary', {})}
- Recent feedback: {context.get('recent_feedback', [])}
- Recent traces: {context.get('recent_traces', [])}
- Dataset split: {context.get('dataset_split', {})}

Current state facts:
{context.get('state', {})}"""
    if role == "executor":
        prior, evaluation, workspace = context.get("prior_context") or {}, context.get("evaluation") or {}, context.get("workspace") or {}
        return f"""Prior context:
- Notes: {prior.get('notes', [])}
- Skills: {prior.get('skills', [])}
- Documents: {prior.get('documents', [])}
- Warnings: {prior.get('warnings', [])}

Evaluation phase: {evaluation.get('eval_phase', 'pareto')}
Execution mode: {evaluation.get('execution_mode')}
Selected sample ids: {evaluation.get('selected_sample_ids', [])}

Candidate decision facts:
{context.get('candidate', {})}

Working directory for any scripts/artifacts you create:
{workspace.get('artifact_dir')}

Candidate source repository:
{workspace.get('source_repo')}"""
    evaluation = context.get("evaluation") or {}
    return f"""Candidate decision facts:
{context.get('candidate', {})}

Trace decision facts:
{context.get('trace', {})}

Evaluation phase: {evaluation.get('eval_phase', 'pareto')}
Selected sample ids: {evaluation.get('selected_sample_ids', [])}"""


_JUDGE_RUBRIC = '''Rubric:
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
- Return actionable feedback that helps the next proposer.'''

_JUDGE_DELIVERY = '''Final delivery contract (mandatory):
- Your final response MUST be exactly one parseable JSON object matching the schema below.
- Wrapping the JSON in a ```json code fence is acceptable; any prose, Markdown table, heading, commentary, or natural-language summary outside the JSON is forbidden.
- NEVER return a judgement report in Markdown.
- If evidence is incomplete, ambiguous, contradictory, or insufficient, still return the JSON object with passed=false, an appropriately low score, failure_categories, actionable_feedback, and confidence="low|medium".
- Do not ask for more data, do not say you will continue later, and do not emit a natural-language wrap-up.

Required JSON schema:
{
  "score": 0.0,
  "passed": false,
  "per_sample_scores": [{"sample_id": "task_execution", "score": 0.0, "notes": ""}],
  "failure_categories": ["category"],
  "actionable_feedback": ["specific next action"],
  "confidence": "low|medium|high"
}'''
