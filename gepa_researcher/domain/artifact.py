from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class ArtifactKind(str, Enum):
    RAW_AGENT_RESPONSE = "raw_agent_response"
    STDOUT = "stdout"
    STDERR = "stderr"
    GIT_DIFF = "git_diff"
    TEST_REPORT = "test_report"
    METRICS = "metrics"
    BENCHMARK = "benchmark"
    EXECUTION_TRACE = "execution_trace"
    SUBMISSION = "submission"
    OTHER = "other"


@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: str
    execution_id: str
    kind: ArtifactKind
    path: str
    sha256: str | None = None
    size_bytes: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["kind"] = self.kind.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ArtifactRef":
        return cls(
            artifact_id=str(data["artifact_id"]),
            execution_id=str(data["execution_id"]),
            kind=ArtifactKind(str(data["kind"])),
            path=str(data["path"]),
            sha256=data.get("sha256"),
            size_bytes=data.get("size_bytes"),
            metadata=dict(data.get("metadata") or {}),
        )
