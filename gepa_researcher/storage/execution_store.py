from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from .io_utils import append_jsonl, read_json, write_json
from ..domain.execution import ExecutionFailure, ExecutionFailureCode, ExecutionPhase, ExecutionRecord, ExecutionSpec, ExecutionStatus
from .event_store import EventStore


_ACTIVE_STATUSES = {
    ExecutionStatus.PENDING,
    ExecutionStatus.PREPARING,
    ExecutionStatus.RUNNING,
    ExecutionStatus.COLLECTING,
}

_TERMINAL_STATUSES = {
    ExecutionStatus.SUCCEEDED,
    ExecutionStatus.FAILED,
    ExecutionStatus.CANCELLED,
}

_ALLOWED_TRANSITIONS = {
    ExecutionStatus.PENDING: {ExecutionStatus.PREPARING, ExecutionStatus.FAILED, ExecutionStatus.CANCELLED},
    ExecutionStatus.PREPARING: {ExecutionStatus.RUNNING, ExecutionStatus.FAILED, ExecutionStatus.CANCELLED},
    ExecutionStatus.RUNNING: {ExecutionStatus.COLLECTING, ExecutionStatus.FAILED, ExecutionStatus.CANCELLED},
    ExecutionStatus.COLLECTING: {ExecutionStatus.SUCCEEDED, ExecutionStatus.FAILED, ExecutionStatus.CANCELLED},
}


class ExecutionStore:
    def __init__(self, run_dir: Path, event_store: EventStore | None = None):
        self.run_dir = Path(run_dir)
        self.root = self.run_dir / "executions"
        self.index_path = self.run_dir / "executions.jsonl"
        self.event_store = event_store
        self._lock = threading.RLock()

    def create_pending(self, spec: ExecutionSpec) -> ExecutionRecord:
        with self._lock:
            record = ExecutionRecord.from_spec(spec)
            self.save(record)
            self._emit("execution.created", record, payload={"status": record.status.value})
            return record

    def save(self, record: ExecutionRecord) -> None:
        payload = record.to_dict()
        with self._lock:
            write_json(self.root / f"{record.execution_id}.json", payload)
            append_jsonl(self.index_path, payload)

    def get(self, execution_id: str) -> ExecutionRecord | None:
        with self._lock:
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
        with self._lock:
            record = self._required(execution_id)
            next_status = ExecutionStatus(status)
            _assert_transition(record.status, next_status)
            previous = record.status
            record.status = next_status
            self.save(record)
            self._emit(
                "execution.status_changed",
                record,
                payload={"from": previous.value, "to": next_status.value},
            )
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
        with self._lock:
            record = self._required(execution_id)
            _assert_transition(record.status, ExecutionStatus.SUCCEEDED)
            previous = record.status
            record.status = ExecutionStatus.SUCCEEDED
            record.result_revision = result_revision
            record.metrics = dict(metrics or {})
            if artifact_refs is not None:
                record.artifact_refs = list(artifact_refs)
            self.save(record)
            self._emit(
                "execution.status_changed",
                record,
                payload={"from": previous.value, "to": ExecutionStatus.SUCCEEDED.value, "result_revision": result_revision},
            )
            return record

    def mark_failed(self, execution_id: str, failure: ExecutionFailure) -> ExecutionRecord:
        with self._lock:
            record = self._required(execution_id)
            _assert_transition(record.status, ExecutionStatus.FAILED)
            previous = record.status
            record.status = ExecutionStatus.FAILED
            record.failure = failure
            self.save(record)
            self._emit(
                "execution.status_changed",
                record,
                payload={"from": previous.value, "to": ExecutionStatus.FAILED.value, "failure": failure.to_dict()},
            )
            return record

    def mark_active_interrupted(self, message: str) -> list[ExecutionRecord]:
        with self._lock:
            interrupted: list[ExecutionRecord] = []
            for record in self.list_active():
                failed = self.mark_failed(
                    record.execution_id,
                    ExecutionFailure(
                        code=ExecutionFailureCode.RUN_INTERRUPTED,
                        message=message,
                        retryable=True,
                        details={"previous_status": record.status.value},
                    ),
                )
                interrupted.append(failed)
            return interrupted

    def _required(self, execution_id: str) -> ExecutionRecord:
        record = self.get(execution_id)
        if record is None:
            raise KeyError(f"unknown execution_id: {execution_id}")
        return record

    def _all_records(self) -> list[ExecutionRecord]:
        with self._lock:
            if not self.root.exists():
                return []
            records = [
                ExecutionRecord.from_dict(read_json(path))
                for path in sorted(self.root.glob("*.json"))
            ]
            return sorted(records, key=lambda record: (record.created_at, record.execution_id))

    def _emit(self, event_type: str, record: ExecutionRecord, *, payload: dict[str, Any]) -> None:
        if self.event_store is None:
            return
        self.event_store.append(
            event_type=event_type,
            source="execution_store",
            run_id=record.run_id,
            round_id=record.round_id,
            candidate_id=record.candidate_id,
            execution_id=record.execution_id,
            payload=payload,
        )


def _assert_transition(current: ExecutionStatus, next_status: ExecutionStatus) -> None:
    if current == next_status:
        return
    if current in _TERMINAL_STATUSES:
        raise ValueError(f"execution status is terminal and cannot transition: {current.value}")
    if next_status not in _ALLOWED_TRANSITIONS.get(current, set()):
        raise ValueError(f"illegal execution status transition: {current.value} -> {next_status.value}")
