from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import threading
from pathlib import Path
from typing import Any

from ..storage.io_utils import append_jsonl, read_json, write_json
from .blocks import SourceRef


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class EntityRecord:
    entity_type: str
    entity_id: str
    summary: str
    source_refs: list[SourceRef] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    updated_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "summary": self.summary,
            "source_refs": [source_ref.to_dict() for source_ref in self.source_refs],
            "metadata": dict(self.metadata),
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EntityRecord":
        return cls(
            entity_type=data["entity_type"],
            entity_id=data["entity_id"],
            summary=data["summary"],
            source_refs=[SourceRef.from_dict(dict(item)) for item in data.get("source_refs") or []],
            metadata=dict(data.get("metadata") or {}),
            updated_at=str(data.get("updated_at") or _now_iso()),
        )


class EntityStore:
    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir)
        self.root = self.run_dir / "context" / "entities"
        self.index_path = self.run_dir / "context" / "entities.jsonl"
        self._lock = threading.RLock()

    def upsert(self, record: EntityRecord) -> EntityRecord:
        payload = record.to_dict()
        with self._lock:
            write_json(self.root / record.entity_type / f"{record.entity_id}.json", payload)
            append_jsonl(self.index_path, payload)
        return record

    def get(self, entity_type: str, entity_id: str) -> EntityRecord | None:
        with self._lock:
            path = self.root / entity_type / f"{entity_id}.json"
            if not path.exists():
                return None
            return EntityRecord.from_dict(read_json(path))

    def list_by_type(self, entity_type: str) -> list[EntityRecord]:
        with self._lock:
            entity_root = self.root / entity_type
            if not entity_root.exists():
                return []
            return [
                EntityRecord.from_dict(read_json(path))
                for path in sorted(entity_root.glob("*.json"))
            ]

    def list_all(self) -> list[EntityRecord]:
        with self._lock:
            if not self.root.exists():
                return []
            records = [
                EntityRecord.from_dict(read_json(path))
                for path in sorted(self.root.glob("*/*.json"))
            ]
            return sorted(records, key=lambda record: (record.entity_type, record.entity_id))
