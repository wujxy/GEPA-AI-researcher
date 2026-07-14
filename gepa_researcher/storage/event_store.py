from __future__ import annotations

import uuid
import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .io_utils import append_jsonl


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class EventRecord:
    event_id: str
    sequence: int
    event_type: str
    source: str
    run_id: str | None = None
    round_id: int | None = None
    candidate_id: str | None = None
    execution_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EventRecord":
        return cls(
            event_id=str(data["event_id"]),
            sequence=int(data["sequence"]),
            event_type=str(data["event_type"]),
            source=str(data["source"]),
            run_id=data.get("run_id"),
            round_id=data.get("round_id"),
            candidate_id=data.get("candidate_id"),
            execution_id=data.get("execution_id"),
            payload=dict(data.get("payload") or {}),
            created_at=str(data.get("created_at") or _now_iso()),
        )


class EventStore:
    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir)
        self.index_path = self.run_dir / "events.jsonl"
        self._lock = threading.RLock()

    def append(
        self,
        *,
        event_type: str,
        source: str,
        run_id: str | None = None,
        round_id: int | None = None,
        candidate_id: str | None = None,
        execution_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> EventRecord:
        with self._lock:
            event = EventRecord(
                event_id=f"event-{uuid.uuid4().hex}",
                sequence=len(self.list_all()) + 1,
                event_type=event_type,
                source=source,
                run_id=run_id,
                round_id=round_id,
                candidate_id=candidate_id,
                execution_id=execution_id,
                payload=dict(payload or {}),
            )
            append_jsonl(self.index_path, event.to_dict())
            return event

    def get(self, event_id: str) -> EventRecord | None:
        return next((event for event in self.list_all() if event.event_id == event_id), None)

    def list_all(self) -> list[EventRecord]:
        with self._lock:
            if not self.index_path.exists():
                return []
            return [
                EventRecord.from_dict(json.loads(line))
                for line in self.index_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

    def list_for_candidate(self, candidate_id: str) -> list[EventRecord]:
        return [event for event in self.list_all() if event.candidate_id == candidate_id]

    def list_for_execution(self, execution_id: str) -> list[EventRecord]:
        return [event for event in self.list_all() if event.execution_id == execution_id]
