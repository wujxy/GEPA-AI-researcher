from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import threading

from ..storage.io_utils import append_jsonl


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class FileCacheKey:
    repo_id: str
    commit_sha: str
    path: str
    content_hash: str

    def to_dict(self) -> dict[str, str]:
        return {
            "repo_id": self.repo_id,
            "commit_sha": self.commit_sha,
            "path": self.path,
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> "FileCacheKey":
        return cls(
            repo_id=data["repo_id"],
            commit_sha=data["commit_sha"],
            path=data["path"],
            content_hash=data["content_hash"],
        )


@dataclass(frozen=True)
class FileRecord:
    key: FileCacheKey
    size_bytes: int
    language: str
    content_ref: str
    summary: str
    symbols: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    updated_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key.to_dict(),
            "size_bytes": self.size_bytes,
            "language": self.language,
            "content_ref": self.content_ref,
            "summary": self.summary,
            "symbols": list(self.symbols),
            "dependencies": list(self.dependencies),
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "FileRecord":
        return cls(
            key=FileCacheKey.from_dict(dict(data["key"])),
            size_bytes=int(data["size_bytes"]),
            language=str(data["language"]),
            content_ref=str(data["content_ref"]),
            summary=str(data["summary"]),
            symbols=[str(item) for item in data.get("symbols") or []],
            dependencies=[str(item) for item in data.get("dependencies") or []],
            updated_at=str(data.get("updated_at") or _now_iso()),
        )


class FileCache:
    _LANGUAGES = {
        ".py": "python",
        ".md": "markdown",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".json": "json",
    }

    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir)
        self.root = self.run_dir / "context" / "file_cache"
        self.content_root = self.root / "content"
        self.index_path = self.root / "index.jsonl"
        self._lock = threading.RLock()

    def put_file(
        self,
        repo_id: str,
        commit_sha: str,
        repo_root: Path,
        path: str,
    ) -> FileRecord:
        relative_path = Path(path)
        source = Path(repo_root) / relative_path
        content = source.read_bytes()
        content_hash = hashlib.sha256(content).hexdigest()
        content_path = self.content_root / f"{content_hash}.txt"
        content_ref = content_path.relative_to(self.run_dir).as_posix()
        record = FileRecord(
            key=FileCacheKey(repo_id, commit_sha, relative_path.as_posix(), content_hash),
            size_bytes=len(content),
            language=self._LANGUAGES.get(relative_path.suffix.lower(), "text"),
            content_ref=content_ref,
            summary=f"File {relative_path.as_posix()}",
        )

        with self._lock:
            self.content_root.mkdir(parents=True, exist_ok=True)
            if not content_path.exists():
                content_path.write_bytes(content)
            append_jsonl(self.index_path, record.to_dict())
        return record

    def get(self, key: FileCacheKey) -> FileRecord | None:
        with self._lock:
            for record in self._records():
                if record.key == key:
                    return record
        return None

    def find_by_path(self, repo_id: str, commit_sha: str, path: str) -> list[FileRecord]:
        normalized_path = Path(path).as_posix()
        with self._lock:
            return [
                record
                for record in self._records()
                if record.key.repo_id == repo_id
                and record.key.commit_sha == commit_sha
                and record.key.path == normalized_path
            ]

    def _records(self) -> list[FileRecord]:
        if not self.index_path.exists():
            return []
        records = []
        for line in self.index_path.read_text(encoding="utf-8").splitlines():
            if line:
                records.append(FileRecord.from_dict(json.loads(line)))
        return records
