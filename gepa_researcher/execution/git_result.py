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
        audit = self._audit(spec, session)
        if audit.commit_count <= 0:
            fallback_files = self._create_fallback_commit_if_possible(spec, session)
            if fallback_files:
                audit = self._audit(spec, session)
                audit.fallback_commit_created = True
                audit.fallback_committed_files = fallback_files
        return audit.result_sha, audit

    def commit_exists(self, session: SandboxSession, commit_sha: str) -> bool:
        if not commit_sha:
            return False
        try:
            _git(session.repo_path, "rev-parse", "--verify", f"{commit_sha}^{{commit}}")
        except GitResultError:
            return False
        return True

    def _audit(self, spec: ExecutionSpec, session: SandboxSession) -> CommitAudit:
        return audit_commit(
            repo=session.repo_path,
            parent_sha=spec.input_revision,
            frozen_globs=list(self.candidate_policy.get("frozen_globs") or []),
        )

    def _create_fallback_commit_if_possible(self, spec: ExecutionSpec, session: SandboxSession) -> list[str]:
        allowed_globs = list(self.candidate_policy.get("allowed_target_globs") or [])
        if not allowed_globs:
            return []
        frozen_globs = list(self.candidate_policy.get("frozen_globs") or [])
        ignored_globs = [*self.readonly_allowed_dirty_globs, *frozen_globs]
        status = _git(session.repo_path, "status", "--porcelain=v1")
        candidate_paths: list[str] = []
        for line in status.splitlines():
            path = _status_path(line)
            if not path:
                continue
            if any(_matches_glob(path, pattern) for pattern in ignored_globs):
                continue
            if any(_matches_glob(path, pattern) for pattern in allowed_globs):
                candidate_paths.append(path)
        candidate_paths = sorted(dict.fromkeys(candidate_paths))
        if not candidate_paths:
            return []
        _git(session.repo_path, "restore", "--staged", "--", ".")
        _git(session.repo_path, "add", "--", *candidate_paths)
        staged = _git(session.repo_path, "diff", "--cached", "--name-only")
        staged_paths = sorted(path for path in staged.splitlines() if path)
        if not staged_paths:
            return []
        message = f"GEPA fallback candidate commit for {spec.candidate_id}"
        _git(
            session.repo_path,
            "-c",
            "user.name=GEPA",
            "-c",
            "user.email=gepa@example.invalid",
            "commit",
            "-m",
            message,
        )
        return staged_paths

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
    if "/**/" in pattern and fnmatch(path, pattern.replace("/**/", "/")):
        return True
    if pattern.startswith("**/") and fnmatch(path, pattern[3:]):
        return True
    if pattern.endswith("/**") and (path == pattern[:-3].rstrip("/") or path.startswith(pattern[:-3])):
        return True
    return False
