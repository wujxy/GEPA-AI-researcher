from __future__ import annotations

from typing import Any

from .schemas import AdmissionDecision, Candidate, DatasetSplit, GateDecision, GenerationDecision, Judgment, LoopState, ParetoFrontier, Trace


def _format_value(value: Any) -> str:
    if value is None:
        return "pending"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def _join_ids(ids: list[str]) -> str:
    return ", ".join(ids) if ids else "none"


def format_run_header(
    task_name: str,
    run_dir: str,
    component_mode: str,
    max_rounds: int,
    batch_size: int,
    dataset_split: DatasetSplit,
) -> str:
    return "\n".join(
        [
            "Run Start",
            f"  task: {task_name}",
            f"  artifacts: {run_dir}",
            f"  components: {component_mode}",
            f"  max_rounds: {max_rounds}",
            f"  batch_size: {batch_size}",
            f"  dataset_split: {dataset_split.to_dict()}",
        ]
    )


def format_run_finish(state: LoopState, final_report_path: str, artifacts_path: str) -> str:
    return "\n".join(
        [
            "Run Finish",
            f"  best_candidate: {state.best_candidate_id}",
            f"  best_score: {state.best_score:.4f}",
            f"  completed_rounds: {len(state.history)}",
            f"  final_report: {final_report_path}",
            f"  artifacts: {artifacts_path}",
        ]
    )


def format_round_header(
    round_id: int,
    max_rounds: int,
    state: LoopState,
    frontier: ParetoFrontier | None = None,
    parents: list[Candidate] | None = None,
) -> str:
    lines = [
        f"Round {round_id + 1}/{max_rounds}",
        f"  best_candidate: {state.best_candidate_id}",
        f"  best_score: {state.best_score:.4f}",
        f"  no_improvement_rounds: {state.no_improvement_rounds}",
    ]
    if frontier is not None:
        lines.append(f"  frontier: {_join_ids(frontier.candidate_ids)}")
        lines.append(f"  selected_parents: {_join_ids(frontier.parent_ids)}")
    if parents is not None:
        lines.append(f"  parent_candidates: {_join_ids([parent.candidate_id for parent in parents])}")
    return "\n".join(lines)


def format_phase_header(
    round_id: int,
    max_rounds: int,
    phase: str,
    sample_ids: list[str] | None = None,
) -> str:
    suffix = f" sample_ids={sample_ids}" if sample_ids is not None else ""
    return f"Phase: {phase} | Round {round_id + 1}/{max_rounds}{suffix}"


def format_agent_action(agent: str, action: str, candidate_id: str | None = None, phase: str | None = None) -> str:
    target = f" {candidate_id}" if candidate_id else ""
    phase_text = f" phase={phase}" if phase else ""
    return f"{agent} {action}{target}{phase_text}"


def format_candidate_list(candidates: list[Candidate]) -> str:
    ids = [candidate.candidate_id for candidate in candidates]
    return "\n".join(["Candidate Batch", f"  count: {len(candidates)}", f"  candidates: {_join_ids(ids)}"])


def format_proposal_summary(
    candidate: Candidate,
    phase: str,
    score: float | None = None,
    role: str | None = None,
) -> str:
    parents = candidate.parent_ids or ([candidate.parent_id] if candidate.parent_id else [])
    instruction = candidate.executor_contract.get("instructions") or "not specified"
    lines = [
        f"Proposal: {candidate.candidate_id}",
        f"  phase: {phase}",
        f"  role: {role or 'candidate'}",
        f"  round: {candidate.round_id}",
        f"  generation: {candidate.generation}",
        f"  status: {candidate.status}",
        f"  parents: {_join_ids([parent for parent in parents if parent])}",
        f"  score: {_format_value(score)}",
        f"  hypothesis: {candidate.hypothesis}",
        f"  change: {candidate.proposed_change}",
        f"  expected: {candidate.expected_improvement}",
        f"  risk: {candidate.risk}",
    ]
    if candidate.mutation_note:
        lines.append(f"  mutation: {candidate.mutation_note}")
    if candidate.merge_note:
        lines.append(f"  merge: {candidate.merge_note}")
    lines.append(f"  executor: {instruction}")
    return "\n".join(lines)


def format_admission_summary(decisions: list[AdmissionDecision]) -> str:
    lines = ["Admission Gate", f"  admitted: {_join_ids([item.candidate_id for item in decisions if item.admitted])}"]
    rejected = [item for item in decisions if not item.admitted]
    lines.append(f"  rejected: {_join_ids([item.candidate_id for item in rejected])}")
    for decision in rejected:
        lines.append(f"  reason[{decision.candidate_id}]: {decision.failure_codes} details={decision.details}")
    return "\n".join(lines)


def format_trace_summary(trace: Trace, phase: str, sample_ids: list[str] | None = None) -> str:
    lines = [
        f"Execution Result: {trace.candidate_id}",
        f"  phase: {phase}",
    ]
    if sample_ids is not None:
        lines.append(f"  sample_ids: {sample_ids}")
    if not trace.samples:
        lines.append("  summary: no samples returned")
        return "\n".join(lines)

    sample = trace.samples[0]
    artifacts = sample.artifacts
    summary = artifacts.get("summary") or sample.logs or sample.output
    lines.append(f"  summary: {summary}")
    for key, label in [
        ("implementation", "implementation"),
        ("metrics", "metrics"),
        ("diagnostics", "diagnostics"),
        ("artifact_paths", "artifact_paths"),
    ]:
        value = artifacts.get(key)
        if value:
            lines.append(f"  {label}: {value}")
    errors = artifacts.get("errors") or sample.error
    if errors:
        lines.append(f"  errors: {errors}")
    return "\n".join(lines)


def format_judgment_summary(judgment: Judgment, phase: str) -> str:
    lines = [
        f"Judgment Result: {judgment.candidate_id}",
        f"  phase: {phase}",
        f"  score: {judgment.score:.4f}",
        f"  passed: {judgment.passed}",
        f"  confidence: {judgment.confidence}",
    ]
    if judgment.failure_categories:
        lines.append(f"  failure_categories: {judgment.failure_categories}")
    if judgment.actionable_feedback:
        lines.append(f"  feedback: {judgment.actionable_feedback}")
    return "\n".join(lines)


def format_gate_summary(decision: GateDecision) -> str:
    lines = [
        "Gate Decision",
        f"  accepted: {_join_ids(decision.accepted)}",
        f"  discarded: {_join_ids(decision.discarded)}",
    ]
    for candidate_id in decision.discarded:
        reason = decision.reason_by_candidate.get(candidate_id, "not specified")
        lines.append(f"  reason[{candidate_id}]: {reason}")
    return "\n".join(lines)


def format_generation_summary(
    decision: GenerationDecision,
    frontier: ParetoFrontier | None = None,
) -> str:
    lines = [
        "Generation Summary",
        f"  kept: {_join_ids(decision.kept)}",
        f"  rejected: {_join_ids(decision.rejected)}",
        f"  stop: {decision.stop}",
        f"  best_candidate: {decision.artifacts.get('best_candidate_id')}",
        f"  best_score: {_format_value(decision.artifacts.get('best_score'))}",
    ]
    if frontier is not None:
        lines.append(f"  frontier_after: {_join_ids(frontier.candidate_ids)}")
    if decision.next_feedback:
        lines.append(f"  next_feedback: {decision.next_feedback}")
    return "\n".join(lines)
