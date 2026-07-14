from __future__ import annotations

import hashlib
import json
import shutil
import threading
import uuid
from pathlib import Path

from .io_utils import append_jsonl, write_json
from ..domain.artifact import ArtifactKind, ArtifactRef


class ArtifactStore:
    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir)
        self.root = self.run_dir / "artifacts"
        self.index_path = self.run_dir / "artifacts.jsonl"
        self._lock = threading.RLock()

    def put(self, execution_id: str, kind: ArtifactKind, file_path: Path) -> ArtifactRef:
        source = Path(file_path).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(f"artifact source is not a file: {source}")
        with self._lock:
            destination_dir = self.root / execution_id
            destination_dir.mkdir(parents=True, exist_ok=True)
            destination = destination_dir / source.name
            if source != destination.resolve():
                shutil.copy2(source, destination)
            ref = ArtifactRef(
                artifact_id=f"artifact-{uuid.uuid4().hex}",
                execution_id=execution_id,
                kind=ArtifactKind(kind),
                path=str(destination.relative_to(self.run_dir)),
                sha256=_sha256(destination),
                size_bytes=destination.stat().st_size,
            )
            append_jsonl(self.index_path, ref.to_dict())
            return ref

    def put_json(
        self,
        execution_id: str,
        kind: ArtifactKind,
        name: str,
        payload: dict,
        metadata: dict | None = None,
    ) -> ArtifactRef:
        safe_name = Path(name).name
        if not safe_name.endswith(".json"):
            safe_name += ".json"
        with self._lock:
            destination = self.root / execution_id / safe_name
            write_json(destination, payload)
            ref = ArtifactRef(
                artifact_id=f"artifact-{uuid.uuid4().hex}",
                execution_id=execution_id,
                kind=ArtifactKind(kind),
                path=str(destination.relative_to(self.run_dir)),
                sha256=_sha256(destination),
                size_bytes=destination.stat().st_size,
                metadata=dict(metadata or {}),
            )
            append_jsonl(self.index_path, ref.to_dict())
            return ref

    def get(self, artifact_id: str) -> ArtifactRef | None:
        return next((artifact for artifact in self.list_all() if artifact.artifact_id == artifact_id), None)

    def list_all(self) -> list[ArtifactRef]:
        with self._lock:
            if not self.index_path.exists():
                return []
            return [
                ArtifactRef.from_dict(json.loads(line))
                for line in self.index_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

    def list_for_execution(self, execution_id: str) -> list[ArtifactRef]:
        return [artifact for artifact in self.list_all() if artifact.execution_id == execution_id]

    def list_by_kind(self, kind: ArtifactKind) -> list[ArtifactRef]:
        kind = ArtifactKind(kind)
        return [artifact for artifact in self.list_all() if artifact.kind == kind]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
