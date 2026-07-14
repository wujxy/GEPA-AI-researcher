from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from ..models.schemas import LoopState
from ..storage.artifact_store import ArtifactStore
from ..storage.candidate_store import CandidateStore
from ..storage.event_store import EventStore
from ..storage.execution_store import ExecutionStore
from ..storage.store import RunStore
from .blocks import ContextBlock, ContextBlockKind, EntityRef, SourceRef
from .entity_store import EntityRecord, EntityStore
from .file_cache import FileCache


class GlobalContextPlane:
    """Build deterministic, provenance-carrying context blocks from run stores."""

    def __init__(
        self,
        run_dir: Path,
        config: dict[str, Any],
        *,
        candidate_store: CandidateStore,
        execution_store: ExecutionStore,
        event_store: EventStore,
        artifact_store: ArtifactStore,
        store: RunStore,
        entity_store: EntityStore | None = None,
        file_cache: FileCache | None = None,
    ):
        self.run_dir = Path(run_dir)
        self.config = deepcopy(config)
        self.candidate_store = candidate_store
        self.execution_store = execution_store
        self.event_store = event_store
        self.artifact_store = artifact_store
        self.store = store
        self.entity_store = entity_store or EntityStore(self.run_dir)
        self.file_cache = file_cache or FileCache(self.run_dir)

    def run_fact_blocks(self, state: LoopState | None = None) -> list[ContextBlock]:
        task = dict(self.config.get("task") or {})
        task_name = str(task.get("name") or (state.task_name if state else "run"))
        content: dict[str, Any] = {
            "task": task,
            "budget": deepcopy(dict(self.config.get("budget") or {})),
        }
        return [
            ContextBlock(
                block_id=f"run:{task_name}",
                kind=ContextBlockKind.RUN_FACT,
                title=f"Run facts: {task_name}",
                summary=str(task.get("goal") or task_name),
                inline_content=content,
                source_refs=[SourceRef(source_type="config", source_id=task_name, path="config.snapshot.json")],
                entity_refs=[],
            )
        ]

    def candidate_blocks(self, candidate_ids: list[str]) -> list[ContextBlock]:
        blocks: list[ContextBlock] = []
        for candidate_id in candidate_ids:
            card = self.candidate_store.get(candidate_id)
            if card is None:
                continue
            source_ref = SourceRef(
                source_type="candidate",
                source_id=card.candidate_id,
                path=f"candidates/{card.candidate_id}.json",
            )
            content = _scrub_agent_raw(_without_timestamps(card.to_dict()))
            block = ContextBlock(
                block_id=f"candidate:{card.candidate_id}",
                kind=ContextBlockKind.CANDIDATE_FACT,
                title=f"Candidate: {card.candidate_id}",
                summary=card.proposal.hypothesis,
                inline_content=content,
                source_refs=[source_ref],
                entity_refs=[EntityRef(entity_type="candidate", entity_id=card.candidate_id)],
            )
            self.entity_store.upsert(
                EntityRecord(
                    entity_type="candidate",
                    entity_id=card.candidate_id,
                    summary=card.proposal.hypothesis,
                    source_refs=[source_ref],
                    metadata={"status": card.status.value, "round_id": card.round_id},
                )
            )
            blocks.append(block)
        return blocks

    def execution_blocks(self, candidate_id: str) -> list[ContextBlock]:
        blocks: list[ContextBlock] = []
        for record in self.execution_store.list_for_candidate(candidate_id):
            source_ref = SourceRef(
                source_type="execution",
                source_id=record.execution_id,
                path=f"executions/{record.execution_id}.json",
            )
            events = [
                _without_timestamps(event.to_dict())
                for event in self.event_store.list_for_execution(record.execution_id)
            ]
            block = ContextBlock(
                block_id=f"execution:{record.execution_id}",
                kind=ContextBlockKind.EXECUTION_FACT,
                title=f"Execution: {record.execution_id}",
                summary=f"{record.phase.value}: {record.status.value}",
                inline_content={"execution": _without_timestamps(record.to_dict()), "events": events},
                source_refs=[
                    source_ref,
                    *[
                        SourceRef(source_type="event", source_id=event["event_id"], path="events.jsonl")
                        for event in events
                    ],
                ],
                entity_refs=[
                    EntityRef(entity_type="candidate", entity_id=record.candidate_id),
                    EntityRef(entity_type="execution", entity_id=record.execution_id),
                ],
            )
            self.entity_store.upsert(
                EntityRecord(
                    entity_type="execution",
                    entity_id=record.execution_id,
                    summary=block.summary or record.execution_id,
                    source_refs=block.source_refs,
                    metadata={
                        "candidate_id": record.candidate_id,
                        "phase": record.phase.value,
                        "status": record.status.value,
                    },
                )
            )
            blocks.append(block)
        return blocks

    def artifact_blocks(self, execution_id: str) -> list[ContextBlock]:
        return [
            ContextBlock(
                block_id=f"artifact:{artifact.artifact_id}",
                kind=ContextBlockKind.ARTIFACT_REF,
                title=f"Artifact: {artifact.artifact_id}",
                summary=f"{artifact.kind.value}: {artifact.path}",
                inline_content=artifact.to_dict(),
                source_refs=[
                    SourceRef(
                        source_type="artifact",
                        source_id=artifact.artifact_id,
                        path=artifact.path,
                    )
                ],
                entity_refs=[EntityRef(entity_type="execution", entity_id=execution_id)],
            )
            for artifact in self.artifact_store.list_for_execution(execution_id)
        ]

    def blocks_for_candidate(self, candidate_id: str) -> list[ContextBlock]:
        blocks = self.candidate_blocks([candidate_id])
        executions = self.execution_blocks(candidate_id)
        blocks.extend(executions)
        for execution in executions:
            execution_id = execution.entity_refs[-1].entity_id
            blocks.extend(self.artifact_blocks(execution_id))
        return blocks


def _without_timestamps(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_timestamps(item)
            for key, item in value.items()
            if key not in {"created_at", "updated_at", "started_at", "finished_at"}
        }
    if isinstance(value, list):
        return [_without_timestamps(item) for item in value]
    return deepcopy(value)


_RAW_CONTEXT_KEYS = {
    "agent_raw",
    "raw",
    "raw_output",
    "original_raw_output",
    "repair_raw_output",
    "repair_raw",
}


def _scrub_agent_raw(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _scrub_agent_raw(item)
            for key, item in value.items()
            if key not in _RAW_CONTEXT_KEYS
        }
    if isinstance(value, list):
        return [_scrub_agent_raw(item) for item in value]
    return deepcopy(value)
