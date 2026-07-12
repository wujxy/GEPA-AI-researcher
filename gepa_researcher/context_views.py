from __future__ import annotations

from pathlib import Path
from typing import Any

from .schemas import Candidate, LoopState, SampleTrace, Trace


ARTIFACT_KEYS_FOR_CONTEXT = {
    "eval_phase",
    "sample_ids",
    "summary",
    "implementation",
    "validation",
    "metrics",
    "diagnostics",
    "artifact_paths",
    "errors",
    "executor_wall_seconds",
    "best_interpretation",
    "execution_mode",
    "execution_record",
    "provenance",
    "agent_call_id",
}


NOISE_KEYS_FOR_CONTEXT = {"agent_raw", "candidate_pool", "parent_executions"}


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
        "parent_ids": list(candidate.parent_ids),
        "generation": candidate.generation,
        "status": candidate.status,
        "hypothesis": candidate.hypothesis,
        "scope": candidate.scope,
        "proposed_change": candidate.proposed_change,
        "rationale": candidate.rationale,
        "expected_improvement": candidate.expected_improvement,
        "risk": candidate.risk,
        "strategy": candidate.strategy or artifacts.get("strategy"),
        "target_files": list(candidate.target_files),
        "safety_class": candidate.safety_class,
        "expected_gain": candidate.expected_gain,
        "admission_status": candidate.admission_status,
        "analysis_plan": artifacts.get("analysis_plan", []),
        "executor_contract": dict(candidate.executor_contract),
        "expected_artifacts": list(candidate.expected_artifacts),
        "mutation_note": candidate.mutation_note,
        "merge_note": candidate.merge_note,
        "evidence_refs": list(evidence_refs or []),
    }


def candidate_for_executor(candidate: Candidate, evidence_refs: list[str] | None = None) -> dict[str, Any]:
    artifacts = dict(candidate.artifacts)
    return {
        "candidate_id": candidate.candidate_id,
        "round_id": candidate.round_id,
        "parent_ids": list(candidate.parent_ids),
        "generation": candidate.generation,
        "hypothesis": candidate.hypothesis,
        "scope": candidate.scope,
        "proposed_change": candidate.proposed_change,
        "strategy": candidate.strategy or artifacts.get("strategy"),
        "target_files": list(candidate.target_files),
        "safety_class": candidate.safety_class,
        "analysis_plan": artifacts.get("analysis_plan", []),
        "executor_contract": dict(candidate.executor_contract),
        "expected_artifacts": list(candidate.expected_artifacts),
        "mutation_note": candidate.mutation_note,
        "evidence_refs": list(evidence_refs or []),
    }


def candidate_for_judger(candidate: Candidate, evidence_refs: list[str] | None = None) -> dict[str, Any]:
    artifacts = dict(candidate.artifacts)
    return {
        "candidate_id": candidate.candidate_id,
        "round_id": candidate.round_id,
        "parent_ids": list(candidate.parent_ids),
        "generation": candidate.generation,
        "hypothesis": candidate.hypothesis,
        "scope": candidate.scope,
        "proposed_change": candidate.proposed_change,
        "risk": candidate.risk,
        "strategy": candidate.strategy or artifacts.get("strategy"),
        "target_files": list(candidate.target_files),
        "safety_class": candidate.safety_class,
        "executor_contract": dict(candidate.executor_contract),
        "expected_artifacts": list(candidate.expected_artifacts),
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


def build_proposer_context(state: LoopState, config: dict[str, Any]) -> dict[str, Any]:
    gepa_context = dict(config.get("_gepa_context") or {})
    frontier = _frontier_for_proposer(dict(gepa_context.get("pareto_frontier") or {}))
    parents = list(gepa_context.get("parents") or [])
    relevant_ids = _relevant_candidate_ids(frontier, parents)
    return {
        "prior_context": _prior_context_for_role(dict(config.get("_prior_context") or {})),
        "state": state_for_agent(state),
        "frontier": frontier,
        "parents": [_candidate_mapping_for_proposer(parent) for parent in parents],
        "score_summary": _score_summary_for_proposer(
            dict(gepa_context.get("score_matrix") or {}),
            relevant_ids,
        ),
        "recent_feedback": list(gepa_context.get("recent_feedback") or []),
        "recent_traces": [
            _recent_trace_for_proposer(trace)
            for trace in (gepa_context.get("recent_traces") or [])
        ],
        "dataset_split": _dataset_split_for_role(dict(gepa_context.get("dataset_split") or {})),
    }


def build_executor_context(
    candidate: Candidate,
    config: dict[str, Any],
    run_dir: Path,
    round_dir: Path,
    repo_dir: Path,
    execution_mode: str,
) -> dict[str, Any]:
    candidate_ref = str(run_dir / "traces" / f"round_{candidate.round_id:03d}" / candidate.candidate_id / "candidate.json")
    return {
        "prior_context": _prior_context_for_role(dict(config.get("_prior_context") or {})),
        "evaluation": {
            "eval_phase": config.get("_eval_phase", "pareto"),
            "execution_mode": execution_mode,
            "selected_sample_ids": list(config.get("_selected_sample_ids") or []),
        },
        "candidate": candidate_for_executor(candidate, [candidate_ref]),
        "workspace": {
            "artifact_dir": str(round_dir),
            "source_repo": str(repo_dir),
        },
    }


def build_judger_context(candidate: Candidate, trace: Trace, config: dict[str, Any]) -> dict[str, Any]:
    run_dir = Path(config.get("_run_dir", "."))
    candidate_ref = str(run_dir / "traces" / f"round_{candidate.round_id:03d}" / candidate.candidate_id / "candidate.json")
    trace_ref = str(run_dir / "traces" / f"round_{trace.round_id:03d}" / trace.candidate_id / "trace.json")
    return {
        "evaluation": {
            "eval_phase": config.get("_eval_phase", "pareto"),
            "selected_sample_ids": list(config.get("_selected_sample_ids") or []),
        },
        "candidate": candidate_for_judger(candidate, [candidate_ref]),
        "trace": trace_for_agent(trace, [trace_ref]),
    }


def _sample_for_agent(sample: SampleTrace) -> dict[str, Any]:
    artifacts = _filtered_artifacts(sample.artifacts)
    return {
        "sample_id": sample.sample_id,
        "logs": sample.logs,
        "error": sample.error,
        "summary": artifacts.get("summary") or sample.logs,
        "implementation": artifacts.get("implementation"),
        "validation": artifacts.get("validation", {}),
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
        "implementation": artifacts.get("implementation"),
        "key_metrics": artifacts.get("metrics", {}),
        "diagnostics": _limit_list(artifacts.get("diagnostics", []), 3),
        "errors": artifacts.get("errors", []),
    }


def _filtered_artifacts(artifacts: dict[str, Any]) -> dict[str, Any]:
    return {key: artifacts[key] for key in ARTIFACT_KEYS_FOR_CONTEXT if key in artifacts}


def _prior_context_for_role(context: dict[str, Any]) -> dict[str, Any]:
    if not context:
        return {}
    return {
        "notes": list(context.get("notes") or []),
        "skills": list(context.get("skills") or []),
        "documents": list(context.get("documents") or []),
        "warnings": list(context.get("warnings") or []),
    }


def _frontier_for_proposer(frontier: dict[str, Any]) -> dict[str, Any]:
    if not frontier:
        return {}
    return {
        "round_id": frontier.get("round_id"),
        "candidate_ids": list(frontier.get("candidate_ids") or []),
        "parent_ids": list(frontier.get("parent_ids") or []),
        "per_task_best": dict(frontier.get("per_task_best") or {}),
    }


def _candidate_mapping_for_proposer(parent: Any) -> dict[str, Any]:
    if isinstance(parent, Candidate):
        return candidate_for_agent(parent)
    data = dict(parent or {})
    artifacts = dict(data.get("artifacts") or {})
    return {
        "candidate_id": data.get("candidate_id"),
        "round_id": data.get("round_id"),
        "parent_ids": list(data.get("parent_ids") or []),
        "generation": data.get("generation"),
        "status": data.get("status"),
        "hypothesis": data.get("hypothesis"),
        "scope": data.get("scope"),
        "proposed_change": data.get("proposed_change"),
        "rationale": data.get("rationale"),
        "expected_improvement": data.get("expected_improvement"),
        "risk": data.get("risk"),
        "strategy": data.get("strategy") or artifacts.get("strategy"),
        "target_files": list(data.get("target_files") or []),
        "safety_class": data.get("safety_class"),
        "expected_gain": data.get("expected_gain"),
        "admission_status": data.get("admission_status"),
        "analysis_plan": artifacts.get("analysis_plan", []),
        "executor_contract": dict(data.get("executor_contract") or {}),
        "expected_artifacts": list(data.get("expected_artifacts") or []),
        "mutation_note": data.get("mutation_note"),
        "merge_note": data.get("merge_note"),
        "evidence_refs": list(data.get("evidence_refs") or []),
    }


def _score_summary_for_proposer(matrix: dict[str, Any], relevant_ids: set[str]) -> dict[str, Any]:
    if not matrix:
        return {}
    aggregate_scores = dict(matrix.get("aggregate_scores") or {})
    if relevant_ids:
        aggregate_scores = {
            candidate_id: score
            for candidate_id, score in aggregate_scores.items()
            if candidate_id in relevant_ids
        }
    task_scores = {}
    for task_id, scores in dict(matrix.get("task_scores") or {}).items():
        filtered = {
            candidate_id: score
            for candidate_id, score in dict(scores or {}).items()
            if not relevant_ids or candidate_id in relevant_ids
        }
        if filtered:
            task_scores[task_id] = filtered
    return {
        "round_id": matrix.get("round_id"),
        "aggregate_scores": aggregate_scores,
        "task_scores": task_scores,
    }


def _recent_trace_for_proposer(trace: Any) -> dict[str, Any]:
    data = _strip_context_noise(trace)
    if not isinstance(data, dict):
        return {}
    samples = []
    for sample in list(data.get("samples") or [])[:3]:
        if not isinstance(sample, dict):
            continue
        artifacts = dict(sample.get("artifacts") or {})
        if artifacts:
            samples.append({
                "sample_id": sample.get("sample_id"),
                "summary": artifacts.get("summary") or sample.get("summary") or sample.get("logs"),
                "error": sample.get("error"),
                "implementation": artifacts.get("implementation") or sample.get("implementation"),
                "key_metrics": artifacts.get("metrics") or sample.get("key_metrics") or sample.get("metrics") or {},
                "diagnostics": _limit_list(artifacts.get("diagnostics") or sample.get("diagnostics") or [], 3),
                "errors": artifacts.get("errors") or sample.get("errors") or [],
            })
        else:
            samples.append(sample)
    return {
        "candidate_id": data.get("candidate_id"),
        "round_id": data.get("round_id"),
        "comparison_to_parent": data.get("comparison_to_parent"),
        "samples": samples,
        "evidence_refs": list(data.get("evidence_refs") or []),
    }


def _dataset_split_for_role(split: dict[str, Any]) -> dict[str, Any]:
    if not split:
        return {}
    artifacts = dict(split.get("artifacts") or {})
    return {
        "feedback_ids": list(split.get("feedback_ids") or []),
        "pareto_ids": list(split.get("pareto_ids") or []),
        "source": artifacts.get("source") or split.get("source"),
    }


def _relevant_candidate_ids(frontier: dict[str, Any], parents: list[Any]) -> set[str]:
    ids = set(map(str, frontier.get("candidate_ids") or []))
    ids.update(map(str, frontier.get("parent_ids") or []))
    for parent in parents:
        if isinstance(parent, Candidate):
            ids.add(parent.candidate_id)
        elif isinstance(parent, dict) and parent.get("candidate_id") is not None:
            ids.add(str(parent.get("candidate_id")))
    return {candidate_id for candidate_id in ids if candidate_id}


def _strip_context_noise(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_context_noise(item)
            for key, item in value.items()
            if key not in NOISE_KEYS_FOR_CONTEXT
        }
    if isinstance(value, list):
        return [_strip_context_noise(item) for item in value]
    return value


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
