from __future__ import annotations

import hashlib
import shutil
import uuid
from pathlib import Path

from .io_utils import append_jsonl
from ..domain.artifact import ArtifactKind, ArtifactRef


class ArtifactStore:
    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir)
        self.root = self.run_dir / "artifacts"
        self.index_path = self.run_dir / "artifacts.jsonl"

    def put(self, execution_id: str, kind: ArtifactKind, file_path: Path) -> ArtifactRef:
        source = Path(file_path).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(f"artifact source is not a file: {source}")
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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
