from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Any


_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


@dataclass(frozen=True)
class RevisionRef:
    commit_sha: str
    repository_id: str = "default"
    parent_sha: str | None = None
    producing_candidate_id: str | None = None
    producing_execution_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "commit_sha", self.validate_sha(self.commit_sha))
        if self.parent_sha is not None:
            object.__setattr__(self, "parent_sha", self.validate_sha(self.parent_sha))

    @staticmethod
    def validate_sha(value: str) -> str:
        text = str(value).strip()
        if not _SHA_RE.fullmatch(text):
            raise ValueError(f"revision must be a 40-character Git commit SHA: {value!r}")
        return text.lower()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RevisionRef":
        return cls(
            commit_sha=str(data["commit_sha"]),
            repository_id=str(data.get("repository_id") or "default"),
            parent_sha=data.get("parent_sha"),
            producing_candidate_id=data.get("producing_candidate_id"),
            producing_execution_id=data.get("producing_execution_id"),
        )
