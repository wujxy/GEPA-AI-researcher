from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .blocks import SourceRef
from ..storage.io_utils import append_jsonl


PRESENTATION_EVENT_TYPES = frozenset(
    {
        "round_started",
        "candidate_proposed",
        "execution_started",
        "candidate_failed",
        "score_changed",
        "run_finished",
    }
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class PresentationEvent:
    event_id: str
    event_type: str
    message: str
    level: str = "info"
    round_id: int | None = None
    candidate_id: str | None = None
    source_refs: list[SourceRef] = field(default_factory=list)
    created_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "message": self.message,
            "level": self.level,
            "round_id": self.round_id,
            "candidate_id": self.candidate_id,
            "source_refs": [source.to_dict() for source in self.source_refs],
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PresentationEvent":
        return cls(
            event_id=str(data["event_id"]),
            event_type=str(data["event_type"]),
            message=str(data["message"]),
            level=str(data.get("level", "info")),
            round_id=data.get("round_id"),
            candidate_id=data.get("candidate_id"),
            source_refs=[SourceRef.from_dict(dict(item)) for item in data.get("source_refs") or []],
            created_at=str(data.get("created_at") or _now_iso()),
        )


class PresentationStream:
    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir)
        self.index_path = self.run_dir / "presentation_events.jsonl"
        self._lock = threading.RLock()

    def append(
        self,
        *,
        event_type: str,
        message: str,
        level: str = "info",
        round_id: int | None = None,
        candidate_id: str | None = None,
        source_refs: list[SourceRef] | None = None,
    ) -> PresentationEvent:
        if event_type not in PRESENTATION_EVENT_TYPES:
            raise ValueError(f"unsupported presentation event type: {event_type!r}")
        event = PresentationEvent(
            event_id=f"presentation-{uuid.uuid4().hex}",
            event_type=event_type,
            message=message,
            level=level,
            round_id=round_id,
            candidate_id=candidate_id,
            source_refs=list(source_refs or []),
        )
        with self._lock:
            append_jsonl(self.index_path, event.to_dict())
        return event

    def list_all(self) -> list[PresentationEvent]:
        with self._lock:
            if not self.index_path.exists():
                return []
            return [
                PresentationEvent.from_dict(json.loads(line))
                for line in self.index_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
