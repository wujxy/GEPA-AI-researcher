from __future__ import annotations

from typing import Any

from .schemas import Candidate, LoopState, SampleTrace, Trace


ARTIFACT_KEYS_FOR_CONTEXT = {
    "eval_phase",
    "sample_ids",
    "summary",
    "model_expression",
    "fit_parameters",
    "metrics",
    "diagnostics",
    "artifact_paths",
    "errors",
    "executor_wall_seconds",
    "best_interpretation",
}


def evidence_access_policy() -> str:
    return (
        "Context evidence policy:\n"
        "- Use structured facts and metrics as the default evidence.\n"
        "- Only read evidence_refs when the structured context is insufficient, ambiguous, or contradictory.\n"
        "- If you read an evidence_ref, use it to resolve the specific missing fact and keep your response compact."
    )


def candidate_for_agent(candidate: Candidate, evidence_refs: list[str] | None = None) -> dict[str, Any]:
    artifacts = dict(candidate.artifacts)
    return {
        "candidate_id": candidate.candidate_id,
        "round_id": candidate.round_id,
        "parent_id": candidate.parent_id,
        "parent_ids": list(candidate.parent_ids),
        "generation": candidate.generation,
        "status": candidate.status,
        "hypothesis": candidate.hypothesis,
        "target_module": candidate.target_module,
        "proposed_change": candidate.proposed_change,
        "rationale": candidate.rationale,
        "expected_improvement": candidate.expected_improvement,
        "risk": candidate.risk,
        "model_family": artifacts.get("model_family"),
        "analysis_plan": artifacts.get("analysis_plan", []),
        "executor_contract": dict(candidate.executor_contract),
        "expected_artifacts": list(candidate.expected_artifacts),
        "mutation_note": candidate.mutation_note,
        "merge_note": candidate.merge_note,
        "evidence_refs": list(evidence_refs or []),
    }


def trace_for_agent(trace: Trace, evidence_refs: list[str] | None = None) -> dict[str, Any]:
    return {
        "candidate_id": trace.candidate_id,
        "round_id": trace.round_id,
        "samples": [_sample_for_agent(sample) for sample in trace.samples],
        "evidence_refs": list(evidence_refs or []),
    }


def trace_summary_for_proposer(
    trace: Trace,
    parent_id: str | None = None,
    parent_score: float | None = None,
    score: float | None = None,
    evidence_refs: list[str] | None = None,
) -> dict[str, Any]:
    comparison = {
        "parent_id": parent_id,
        "parent_score": parent_score,
        "score": score,
        "verdict": _comparison_verdict(score, parent_score),
    }
    return {
        "candidate_id": trace.candidate_id,
        "round_id": trace.round_id,
        "comparison_to_parent": comparison,
        "samples": [_sample_summary_for_proposer(sample) for sample in trace.samples],
        "evidence_refs": list(evidence_refs or []),
    }


def state_for_agent(state: LoopState, history_limit: int = 3) -> dict[str, Any]:
    return {
        "task_name": state.task_name,
        "round_id": state.round_id,
        "best_candidate_id": state.best_candidate_id,
        "best_score": state.best_score,
        "no_improvement_rounds": state.no_improvement_rounds,
        "recent_history": [_history_item_for_agent(item) for item in state.history[-history_limit:]],
    }


def _sample_for_agent(sample: SampleTrace) -> dict[str, Any]:
    artifacts = _filtered_artifacts(sample.artifacts)
    return {
        "sample_id": sample.sample_id,
        "logs": sample.logs,
        "error": sample.error,
        "summary": artifacts.get("summary") or sample.logs,
        "model_expression": artifacts.get("model_expression"),
        "fit_parameters": artifacts.get("fit_parameters", {}),
        "metrics": artifacts.get("metrics", {}),
        "diagnostics": _limit_list(artifacts.get("diagnostics", []), 3),
        "artifact_paths": artifacts.get("artifact_paths", []),
        "errors": artifacts.get("errors", []),
        "executor_wall_seconds": artifacts.get("executor_wall_seconds"),
    }


def _sample_summary_for_proposer(sample: SampleTrace) -> dict[str, Any]:
    artifacts = _filtered_artifacts(sample.artifacts)
    return {
        "sample_id": sample.sample_id,
        "summary": artifacts.get("summary") or sample.logs,
        "error": sample.error,
        "model_expression": artifacts.get("model_expression"),
        "key_metrics": artifacts.get("metrics", {}),
        "diagnostics": _limit_list(artifacts.get("diagnostics", []), 3),
        "errors": artifacts.get("errors", []),
    }


def _filtered_artifacts(artifacts: dict[str, Any]) -> dict[str, Any]:
    return {key: artifacts[key] for key in ARTIFACT_KEYS_FOR_CONTEXT if key in artifacts}


def _limit_list(value: Any, limit: int) -> list[Any]:
    if not isinstance(value, list):
        return []
    return value[:limit]


def _comparison_verdict(score: float | None, parent_score: float | None) -> str:
    if score is None or parent_score is None:
        return "unknown"
    if score > parent_score:
        return "better_than_parent"
    if score == parent_score:
        return "tied_parent"
    return "worse_than_parent"


def _history_item_for_agent(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "round_id": item.get("round_id"),
        "kept": list(item.get("kept", [])),
        "rejected": list(item.get("rejected", [])),
        "best_candidate_id": item.get("best_candidate_id"),
        "best_score": item.get("best_score"),
        "stop": item.get("stop"),
        "next_feedback_count": len(item.get("next_feedback", [])),
    }
