from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from typing import Any


class ContextRole(str, Enum):
    PROPOSER = "proposer"
    EXECUTOR = "executor"
    JUDGE = "judge"
    USER = "user"


class ContextBlockKind(str, Enum):
    RUN_FACT = "run_fact"
    LOOP_STATE = "loop_state"
    CANDIDATE_FACT = "candidate_fact"
    EXECUTION_FACT = "execution_fact"
    JUDGMENT_FACT = "judgment_fact"
    GATE_FACT = "gate_fact"
    ARTIFACT_REF = "artifact_ref"
    FILE_CONTEXT = "file_context"
    DERIVED_SUMMARY = "derived_summary"
    USER_EVENT = "user_event"


class ContextVisibility(str, Enum):
    INTERNAL = "internal"
    AGENT = "agent"
    USER = "user"


class ContextRenderMode(str, Enum):
    FULL = "full"
    SUMMARY = "summary"
    REF = "ref"


@dataclass(frozen=True)
class SourceRef:
    source_type: str
    source_id: str | None
    path: str | None = None
    field: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type,
            "source_id": self.source_id,
            "path": self.path,
            "field": self.field,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SourceRef":
        return cls(
            source_type=data["source_type"],
            source_id=data.get("source_id"),
            path=data.get("path"),
            field=data.get("field"),
        )


@dataclass(frozen=True)
class EntityRef:
    entity_type: str
    entity_id: str

    def to_dict(self) -> dict[str, Any]:
        return {"entity_type": self.entity_type, "entity_id": self.entity_id}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EntityRef":
        return cls(entity_type=data["entity_type"], entity_id=data["entity_id"])


@dataclass(frozen=True)
class ContextBlock:
    block_id: str
    kind: ContextBlockKind
    title: str
    summary: str | None
    inline_content: dict[str, Any]
    source_refs: list[SourceRef]
    entity_refs: list[EntityRef]
    role_scope: list[ContextRole] | None = None
    visibility: ContextVisibility = ContextVisibility.INTERNAL
    render_mode: ContextRenderMode = ContextRenderMode.FULL
    schema_version: str = "context-block-v1"

    def __post_init__(self) -> None:
        if self.kind != ContextBlockKind.RUN_FACT and not self.source_refs:
            raise ValueError("source_refs are required for non-RUN_FACT context blocks")

    def to_dict(self) -> dict[str, Any]:
        return {
            "block_id": self.block_id,
            "kind": self.kind.value,
            "title": self.title,
            "summary": self.summary,
            "inline_content": deepcopy(self.inline_content),
            "source_refs": [source.to_dict() for source in self.source_refs],
            "entity_refs": [entity.to_dict() for entity in self.entity_refs],
            "role_scope": [role.value for role in self.role_scope] if self.role_scope is not None else None,
            "visibility": self.visibility.value,
            "render_mode": self.render_mode.value,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ContextBlock":
        role_scope = data.get("role_scope")
        return cls(
            block_id=data["block_id"],
            kind=ContextBlockKind(data["kind"]),
            title=data["title"],
            summary=data.get("summary"),
            inline_content=dict(data.get("inline_content") or {}),
            source_refs=[SourceRef.from_dict(dict(item)) for item in data.get("source_refs") or []],
            entity_refs=[EntityRef.from_dict(dict(item)) for item in data.get("entity_refs") or []],
            role_scope=[ContextRole(role) for role in role_scope] if role_scope is not None else None,
            visibility=ContextVisibility(data.get("visibility", ContextVisibility.INTERNAL.value)),
            render_mode=ContextRenderMode(data.get("render_mode", ContextRenderMode.FULL.value)),
            schema_version=data.get("schema_version", "context-block-v1"),
        )
