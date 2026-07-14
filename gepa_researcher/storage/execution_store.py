from __future__ import annotations

from pathlib import Path
from typing import Any

from .io_utils import append_jsonl, read_json, write_json
from ..domain.execution import ExecutionFailure, ExecutionPhase, ExecutionRecord, ExecutionSpec, ExecutionStatus


_ACTIVE_STATUSES = {
    ExecutionStatus.PENDING,
    ExecutionStatus.PREPARING,
    ExecutionStatus.RUNNING,
    ExecutionStatus.COLLECTING,
}


class ExecutionStore:
    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir)
        self.root = self.run_dir / "executions"
        self.index_path = self.run_dir / "executions.jsonl"

    def create_pending(self, spec: ExecutionSpec) -> ExecutionRecord:
        record = ExecutionRecord.from_spec(spec)
        self.save(record)
        return record

    def save(self, record: ExecutionRecord) -> None:
        payload = record.to_dict()
        write_json(self.root / f"{record.execution_id}.json", payload)
        append_jsonl(self.index_path, payload)

    def get(self, execution_id: str) -> ExecutionRecord | None:
        path = self.root / f"{execution_id}.json"
        if not path.exists():
            return None
        return ExecutionRecord.from_dict(read_json(path))

    def list_for_candidate(self, candidate_id: str) -> list[ExecutionRecord]:
        return [
            record
            for record in self._all_records()
            if record.candidate_id == candidate_id
        ]

    def list_active(self) -> list[ExecutionRecord]:
        return [
            record
            for record in self._all_records()
            if record.status in _ACTIVE_STATUSES
        ]

    def list_by_phase(self, candidate_id: str, phase: ExecutionPhase) -> list[ExecutionRecord]:
        phase = ExecutionPhase(phase)
        return [
            record
            for record in self.list_for_candidate(candidate_id)
            if record.phase == phase
        ]

    def mark_status(self, execution_id: str, status: ExecutionStatus) -> ExecutionRecord:
        record = self._required(execution_id)
        record.status = ExecutionStatus(status)
        self.save(record)
        return record

    def mark_preparing(self, execution_id: str) -> ExecutionRecord:
        return self.mark_status(execution_id, ExecutionStatus.PREPARING)

    def mark_running(self, execution_id: str) -> ExecutionRecord:
        return self.mark_status(execution_id, ExecutionStatus.RUNNING)

    def mark_collecting(self, execution_id: str) -> ExecutionRecord:
        return self.mark_status(execution_id, ExecutionStatus.COLLECTING)

    def mark_succeeded(
        self,
        execution_id: str,
        *,
        result_revision: str | None = None,
        metrics: dict[str, float] | None = None,
        artifact_refs: list[Any] | None = None,
    ) -> ExecutionRecord:
        record = self._required(execution_id)
        record.status = ExecutionStatus.SUCCEEDED
        record.result_revision = result_revision
        record.metrics = dict(metrics or {})
        if artifact_refs is not None:
            record.artifact_refs = list(artifact_refs)
        self.save(record)
        return record

    def mark_failed(self, execution_id: str, failure: ExecutionFailure) -> ExecutionRecord:
        record = self._required(execution_id)
        record.status = ExecutionStatus.FAILED
        record.failure = failure
        self.save(record)
        return record

    def _required(self, execution_id: str) -> ExecutionRecord:
        record = self.get(execution_id)
        if record is None:
            raise KeyError(f"unknown execution_id: {execution_id}")
        return record

    def _all_records(self) -> list[ExecutionRecord]:
        if not self.root.exists():
            return []
        records = [
            ExecutionRecord.from_dict(read_json(path))
            for path in sorted(self.root.glob("*.json"))
        ]
        return sorted(records, key=lambda record: (record.created_at, record.execution_id))
