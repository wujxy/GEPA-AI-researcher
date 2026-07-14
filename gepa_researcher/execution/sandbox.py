from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SandboxSession:
    execution_id: str
    repo_path: Path
    artifact_path: Path
    scratch_path: Path
    input_revision: str
    mode: str
    temporary_paths: tuple[Path, ...] = ()
    controller_repo_path: Path | None = None
    branch_name: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "repo_path": str(self.repo_path),
            "artifact_path": str(self.artifact_path),
            "scratch_path": str(self.scratch_path),
            "input_revision": self.input_revision,
            "mode": self.mode,
            "temporary_paths": [str(path) for path in self.temporary_paths],
            "controller_repo_path": str(self.controller_repo_path) if self.controller_repo_path else None,
            "branch_name": self.branch_name,
            "metadata": dict(self.metadata),
        }
