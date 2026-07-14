from __future__ import annotations

import subprocess
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from ..domain.execution import ExecutionPhase, ExecutionSpec
from ..models.schemas import CommitAudit
from ..storage.provenance import audit_commit
from .sandbox import SandboxSession


class GitResultError(RuntimeError):
    pass


class GitResultService:
    def __init__(self, candidate_policy: dict[str, Any] | None = None):
        self.candidate_policy = dict(candidate_policy or {})
        self.readonly_allowed_dirty_globs = list(self.candidate_policy.get("readonly_allowed_dirty_globs") or [])

    def finalize_implementation(self, spec: ExecutionSpec, session: SandboxSession) -> tuple[str | None, CommitAudit]:
        if spec.phase != ExecutionPhase.IMPLEMENTATION:
            raise GitResultError(f"finalize_implementation requires implementation phase, got {spec.phase.value}")
        audit = audit_commit(
            repo=session.repo_path,
            parent_sha=spec.input_revision,
            frozen_globs=list(self.candidate_policy.get("frozen_globs") or []),
        )
        return audit.result_sha, audit

    def snapshot(self, session: SandboxSession) -> dict[str, str]:
        return {
            "head": _git(session.repo_path, "rev-parse", "HEAD"),
            "tracked_status": _git(session.repo_path, "status", "--porcelain=v1", "--untracked-files=no"),
        }

    def assert_readonly_unchanged(
        self,
        spec: ExecutionSpec,
        session: SandboxSession,
        before: dict[str, str] | None,
    ) -> None:
        if spec.phase == ExecutionPhase.IMPLEMENTATION:
            raise GitResultError("readonly guard cannot be used for implementation phase")
        if before is None:
            before = self.snapshot(session)
        after = self.snapshot(session)
        if after["head"] != before["head"] or not _readonly_status_allowed(
            before.get("tracked_status", ""),
            after.get("tracked_status", ""),
            self.readonly_allowed_dirty_globs,
        ):
            raise GitResultError(
                "read-only execution changed sandbox: "
                f"execution_id={spec.execution_id} before={before} after={after}"
            )


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise GitResultError(f"git {' '.join(args)} failed: {completed.stderr.strip()}")
    return completed.stdout.strip()


def _readonly_status_allowed(before: str, after: str, allowed_globs: list[str]) -> bool:
    before_lines = set(before.splitlines())
    for line in after.splitlines():
        if line in before_lines:
            continue
        path = _status_path(line)
        if not any(_matches_glob(path, pattern) for pattern in allowed_globs):
            return False
    return True


def _status_path(line: str) -> str:
    if len(line) > 2 and line[2] == " ":
        path = line[3:]
    elif len(line) > 1 and line[1] == " ":
        path = line[2:]
    else:
        path = line[3:] if len(line) > 3 else ""
    if " -> " in path:
        path = path.rsplit(" -> ", 1)[-1]
    return path.strip()


def _matches_glob(path: str, pattern: str) -> bool:
    if fnmatch(path, pattern):
        return True
    if pattern.endswith("/**") and (path == pattern[:-3].rstrip("/") or path.startswith(pattern[:-3])):
        return True
    return False
