from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..models.schemas import Candidate, ContextEnvelope, LoopState, Trace
from .blocks import (
    ContextBlock,
    ContextBlockKind,
    ContextRole,
    ContextVisibility,
    SourceRef,
)
from .plane import GlobalContextPlane


@dataclass(frozen=True)
class ContextView:
    role: ContextRole
    envelope: ContextEnvelope
    blocks: list[ContextBlock]
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role.value,
            "envelope": self.envelope.to_dict(),
            "blocks": [block.to_dict() for block in self.blocks],
            "metadata": deepcopy(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ContextView":
        return cls(
            role=ContextRole(str(data["role"])),
            envelope=ContextEnvelope(**dict(data["envelope"])),
            blocks=[ContextBlock.from_dict(dict(block)) for block in data.get("blocks") or []],
            metadata=dict(data.get("metadata") or {}),
        )


class ContextViewBuilder:
    """Build deterministic, role-scoped views from the global context plane."""

    def __init__(self, plane: GlobalContextPlane):
        self.plane = plane

    def for_proposer(
        self,
        state: LoopState,
        frontier: Any = None,
        parent_ids: list[str] | None = None,
    ) -> ContextView:
        selected_parent_ids = _dedupe_ordered(parent_ids or _frontier_ids(frontier))
        blocks = [
            *_for_role(self.plane.run_fact_blocks(), ContextRole.PROPOSER),
            _loop_state_block(state, ContextRole.PROPOSER),
            *_for_role(self.plane.candidate_blocks(selected_parent_ids), ContextRole.PROPOSER),
            *_recent_judgment_blocks(self.plane, selected_parent_ids, ContextRole.PROPOSER),
        ]
        return ContextView(
            role=ContextRole.PROPOSER,
            envelope=_envelope(ContextRole.PROPOSER, state.round_id, self.plane.config),
            blocks=_ordered(blocks),
            metadata={"frontier": _metadata_value(frontier), "parent_ids": selected_parent_ids},
        )

    def for_executor(
        self,
        candidate: Candidate,
        config: dict[str, Any],
        run_dir: Path,
        round_dir: Path,
        repo_dir: Path,
        execution_mode: str,
    ) -> ContextView:
        role = ContextRole.EXECUTOR
        blocks = [
            *_for_role(self.plane.run_fact_blocks(), role),
            _candidate_block(candidate, role),
            _workspace_block(candidate.candidate_id, round_dir, repo_dir, role),
            _evaluation_block(candidate.candidate_id, config, execution_mode, role),
            *_execution_summary_blocks(self.plane, candidate.candidate_id, role),
        ]
        return ContextView(
            role=role,
            envelope=_envelope(role, candidate.round_id, config, candidate_id=candidate.candidate_id),
            blocks=_ordered(blocks),
            metadata={"run_dir": str(run_dir)},
        )

    def for_judge(self, candidate: Candidate, trace: Trace, config: dict[str, Any]) -> ContextView:
        role = ContextRole.JUDGE
        blocks = [
            *_for_role(self.plane.run_fact_blocks(), role),
            _candidate_block(candidate, role, judge_safe=True),
            _trace_summary_block(trace, role),
            *_execution_summary_blocks(self.plane, candidate.candidate_id, role),
            *_for_role(_artifact_blocks_for_candidate(self.plane, candidate.candidate_id), role),
        ]
        return ContextView(
            role=role,
            envelope=_envelope(role, trace.round_id, config, candidate_id=candidate.candidate_id),
            blocks=_ordered(blocks),
            metadata={"evaluation_phase": config.get("_eval_phase", "pareto")},
        )


def _envelope(
    role: ContextRole,
    round_id: int,
    config: dict[str, Any],
    *,
    candidate_id: str | None = None,
) -> ContextEnvelope:
    return ContextEnvelope(
        role=role.value,
        round_id=round_id,
        phase=str(config.get("_eval_phase") or config.get("_agent_phase") or "proposal"),
        run_id=config.get("run_id"),
        candidate_id=candidate_id,
        execution_id=config.get("_execution_id"),
        input_revision=config.get("_input_revision"),
        selected_sample_ids=list(config.get("_selected_sample_ids") or []),
    )


def _for_role(blocks: list[ContextBlock], role: ContextRole) -> list[ContextBlock]:
    return [
        replace(
            block,
            inline_content=deepcopy(block.inline_content),
            role_scope=[role],
            visibility=ContextVisibility.AGENT,
        )
        for block in blocks
    ]


def _loop_state_block(state: LoopState, role: ContextRole) -> ContextBlock:
    return ContextBlock(
        block_id=f"loop-state:{state.task_name}",
        kind=ContextBlockKind.LOOP_STATE,
        title="Loop state",
        summary=f"round {state.round_id}",
        inline_content=state.to_dict(),
        source_refs=[SourceRef(source_type="loop_state", source_id=state.task_name, path="state.json")],
        entity_refs=[],
        role_scope=[role],
        visibility=ContextVisibility.AGENT,
    )


def _candidate_block(candidate: Candidate, role: ContextRole, *, judge_safe: bool = False) -> ContextBlock:
    content = _judge_safe_candidate_content(candidate) if judge_safe else candidate.to_dict()
    return ContextBlock(
        block_id=f"candidate:{candidate.candidate_id}",
        kind=ContextBlockKind.CANDIDATE_FACT,
        title=f"Candidate: {candidate.candidate_id}",
        summary=candidate.hypothesis,
        inline_content=content,
        source_refs=[
            SourceRef(
                source_type="candidate",
                source_id=candidate.candidate_id,
                path=f"traces/round_{candidate.round_id:03d}/{candidate.candidate_id}/candidate.json",
            )
        ],
        entity_refs=[],
        role_scope=[role],
        visibility=ContextVisibility.AGENT,
    )


def _judge_safe_candidate_content(candidate: Candidate) -> dict[str, Any]:
    """Project only the candidate evidence that legacy judges receive."""
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
    }


def _workspace_block(candidate_id: str, round_dir: Path, repo_dir: Path, role: ContextRole) -> ContextBlock:
    return ContextBlock(
        block_id=f"workspace:{candidate_id}",
        kind=ContextBlockKind.DERIVED_SUMMARY,
        title="Workspace",
        summary=str(repo_dir),
        inline_content={"artifact_dir": str(round_dir), "source_repo": str(repo_dir)},
        source_refs=[SourceRef(source_type="workspace", source_id=candidate_id, path=str(round_dir))],
        entity_refs=[],
        role_scope=[role],
        visibility=ContextVisibility.AGENT,
    )


def _evaluation_block(candidate_id: str, config: dict[str, Any], execution_mode: str, role: ContextRole) -> ContextBlock:
    return ContextBlock(
        block_id=f"evaluation:{candidate_id}",
        kind=ContextBlockKind.DERIVED_SUMMARY,
        title="Evaluation selection",
        summary=str(config.get("_eval_phase", "pareto")),
        inline_content={
            "eval_phase": config.get("_eval_phase", "pareto"),
            "execution_mode": execution_mode,
            "selected_sample_ids": list(config.get("_selected_sample_ids") or []),
        },
        source_refs=[SourceRef(source_type="config", source_id=candidate_id, path="config.snapshot.json")],
        entity_refs=[],
        role_scope=[role],
        visibility=ContextVisibility.AGENT,
    )


def _execution_summary_blocks(plane: GlobalContextPlane, candidate_id: str, role: ContextRole) -> list[ContextBlock]:
    summaries: list[ContextBlock] = []
    for block in plane.execution_blocks(candidate_id):
        summaries.append(
            replace(
                block,
                inline_content={"execution": deepcopy(block.inline_content["execution"])},
                role_scope=[role],
                visibility=ContextVisibility.AGENT,
            )
        )
    return summaries


def _artifact_blocks_for_candidate(plane: GlobalContextPlane, candidate_id: str) -> list[ContextBlock]:
    blocks: list[ContextBlock] = []
    for execution in plane.execution_blocks(candidate_id):
        execution_id = execution.entity_refs[-1].entity_id
        blocks.extend(plane.artifact_blocks(execution_id))
    return blocks


def _trace_summary_block(trace: Trace, role: ContextRole) -> ContextBlock:
    samples = []
    for sample in trace.samples:
        artifacts = dict(sample.artifacts)
        samples.append(
            {
                "sample_id": sample.sample_id,
                "error": sample.error,
                "latency_ms": sample.latency_ms,
                "summary": artifacts.get("summary"),
                "metrics": deepcopy(dict(artifacts.get("metrics") or {})),
                "diagnostics": list(artifacts.get("diagnostics") or [])[:3],
            }
        )
    return ContextBlock(
        block_id=f"trace:{trace.candidate_id}:round:{trace.round_id}",
        kind=ContextBlockKind.DERIVED_SUMMARY,
        title=f"Trace summary: {trace.candidate_id}",
        summary=f"{len(trace.samples)} samples",
        inline_content={"candidate_id": trace.candidate_id, "round_id": trace.round_id, "samples": samples},
        source_refs=[
            SourceRef(
                source_type="trace",
                source_id=trace.candidate_id,
                path=f"traces/round_{trace.round_id:03d}/{trace.candidate_id}/trace.json",
            )
        ],
        entity_refs=[],
        role_scope=[role],
        visibility=ContextVisibility.AGENT,
    )


def _recent_judgment_blocks(plane: GlobalContextPlane, candidate_ids: list[str], role: ContextRole) -> list[ContextBlock]:
    blocks: list[ContextBlock] = []
    for candidate_id in candidate_ids:
        for event in plane.event_store.list_for_candidate(candidate_id):
            if "judg" not in event.event_type.lower():
                continue
            blocks.append(
                ContextBlock(
                    block_id=f"judgment:{event.event_id}",
                    kind=ContextBlockKind.JUDGMENT_FACT,
                    title=f"Judge feedback: {candidate_id}",
                    summary=event.event_type,
                    inline_content=deepcopy(event.payload),
                    source_refs=[SourceRef(source_type="event", source_id=event.event_id, path="events.jsonl")],
                    entity_refs=[],
                    role_scope=[role],
                    visibility=ContextVisibility.AGENT,
                )
            )
    return blocks


def _frontier_ids(frontier: Any) -> list[str]:
    if hasattr(frontier, "to_dict"):
        return _frontier_ids(frontier.to_dict())
    if isinstance(frontier, dict):
        return list(frontier.get("parent_ids") or frontier.get("candidate_ids") or [])
    if isinstance(frontier, (list, tuple, set)):
        return list(frontier)
    return []


def _dedupe_ordered(values: Any) -> list[str]:
    selected: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        item = str(value)
        if item in seen:
            continue
        selected.append(item)
        seen.add(item)
    return selected


def _metadata_value(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return _metadata_value(value.to_dict())
    if isinstance(value, dict):
        return {key: _metadata_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_metadata_value(item) for item in value]
    if isinstance(value, tuple):
        return [_metadata_value(item) for item in value]
    if isinstance(value, set):
        return sorted(_metadata_value(item) for item in value)
    return deepcopy(value)


def _ordered(blocks: list[ContextBlock]) -> list[ContextBlock]:
    return sorted(blocks, key=lambda block: block.block_id)
