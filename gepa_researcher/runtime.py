from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from .schemas import DatasetSplit


def all_sample_ids(config: dict[str, Any]) -> list[str]:
    samples = config.get("task", {}).get("samples") or []
    ids = [str(sample.get("sample_id")) for sample in samples if sample.get("sample_id")]
    return ids or ["observed_numeric_dataset"]


def resolve_dataset_split(config: dict[str, Any]) -> DatasetSplit:
    gepa = config.get("gepa", {})
    ids = all_sample_ids(config)
    feedback_ids = [str(item) for item in gepa.get("feedback_sample_ids", [])]
    pareto_ids = [str(item) for item in gepa.get("pareto_sample_ids", [])]
    if not feedback_ids or not pareto_ids:
        if len(ids) <= 1:
            feedback_ids = feedback_ids or list(ids)
            pareto_ids = pareto_ids or list(ids)
        else:
            minibatch = max(1, int(gepa.get("minibatch_size", 1)))
            cut = min(max(1, len(ids) // 2), len(ids) - 1, minibatch)
            feedback_ids = feedback_ids or ids[:cut]
            pareto_ids = pareto_ids or ids[cut:]
    return DatasetSplit(
        feedback_ids=list(dict.fromkeys(feedback_ids)),
        pareto_ids=list(dict.fromkeys(pareto_ids)),
        artifacts={"source": "config" if gepa.get("feedback_sample_ids") or gepa.get("pareto_sample_ids") else "deterministic"},
    )


def select_feedback_minibatch(split: DatasetSplit, round_id: int, minibatch_size: int) -> list[str]:
    ids = split.feedback_ids or split.pareto_ids
    if not ids:
        return []
    size = max(1, min(int(minibatch_size), len(ids)))
    start = (round_id * size) % len(ids)
    rotated = ids[start:] + ids[:start]
    return rotated[:size]


def config_for_eval(
    config: dict[str, Any],
    sample_ids: list[str],
    phase: str,
    prior_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    selected = set(sample_ids)
    next_config = deepcopy(config)
    next_config["_eval_phase"] = phase
    next_config["_selected_sample_ids"] = list(sample_ids)
    if prior_context is not None:
        next_config["_prior_context"] = prior_context
    samples = next_config.get("task", {}).get("samples")
    if samples:
        next_config["task"]["samples"] = [sample for sample in samples if str(sample.get("sample_id")) in selected]
    return next_config


def recent_trace_summaries(run_dir: Path, limit: int = 5) -> list[dict[str, Any]]:
    path = run_dir / "traces.jsonl"
    if not path.exists():
        return []
    rows = path.read_text(encoding="utf-8").splitlines()[-limit:]
    summaries: list[dict[str, Any]] = []
    for row in rows:
        try:
            import json
            data = json.loads(row)
        except Exception:
            continue
        samples = data.get("samples") or []
        summaries.append(
            {
                "candidate_id": data.get("candidate_id"),
                "round_id": data.get("round_id"),
                "samples": [
                    {
                        "sample_id": sample.get("sample_id"),
                        "logs": sample.get("logs"),
                        "error": sample.get("error"),
                        "artifacts": sample.get("artifacts", {}),
                    }
                    for sample in samples[:3]
                ],
            }
        )
    return summaries
