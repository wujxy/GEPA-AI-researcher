from __future__ import annotations

import fnmatch
import hashlib
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .schemas import Candidate, ExecutionRecord, ProvenanceReport, WorkspaceLease


class ProvenanceVerifier:
    def verify(
        self,
        candidate: Candidate,
        lease: WorkspaceLease,
        record: ExecutionRecord,
        config: dict[str, Any],
    ) -> ProvenanceReport:
        if lease.mode != "git_worktree":
            return ProvenanceReport(
                execution_id=record.execution_id,
                candidate_id=candidate.candidate_id,
                verified=True,
                checks={"mode": "not_applicable"},
                result_sha=record.result_sha,
            )

        repo = Path(lease.worktree_path)
        workspace_config = dict(config.get("workspace") or {})
        policy = dict(config.get("candidate_policy") or {})
        checks: dict[str, str] = {}
        codes: list[str] = []
        details: list[str] = []
        head = _git(repo, "rev-parse", "HEAD")
        record.result_sha = head

        if record.actual_start_sha != lease.requested_parent_sha:
            codes.append("START_SHA_MISMATCH")
            details.append(f"{record.actual_start_sha} != {lease.requested_parent_sha}")
            checks["start_sha"] = "fail"
        else:
            checks["start_sha"] = "pass"

        ancestor = _git_rc(repo, "merge-base", "--is-ancestor", lease.requested_parent_sha, head) == 0
        if not ancestor:
            codes.append("PARENT_NOT_ANCESTOR")
            checks["ancestry"] = "fail"
        else:
            checks["ancestry"] = "pass"

        commit_count = int(_git(repo, "rev-list", "--count", f"{lease.requested_parent_sha}..{head}") or 0)
        record.commit_count = commit_count
        max_commits = int(policy.get("max_commits", 1))
        expected_max = 0 if record.execution_mode == "evaluate_only" else max_commits
        if commit_count > expected_max:
            codes.append("COMMIT_BUDGET_EXCEEDED")
            details.append(f"{commit_count} commits exceeds {expected_max}")
            checks["commit_count"] = "fail"
        else:
            checks["commit_count"] = "pass"

        changed = [
            line for line in _git(repo, "diff", "--name-only", lease.requested_parent_sha, head).splitlines() if line
        ]
        record.changed_files = changed
        target_patterns = list(candidate.target_files)
        invalid_changed = [
            path for path in changed if target_patterns and not any(fnmatch.fnmatch(path, target) for target in target_patterns)
        ]
        if invalid_changed:
            codes.append("CHANGED_PATH_NOT_ADMITTED")
            details.extend(invalid_changed)
            checks["changed_files"] = "fail"
        else:
            checks["changed_files"] = "pass"

        dirty = _dirty_paths(repo)
        generated = list(workspace_config.get("generated_tracked_paths", []))
        invalid_dirty = [
            path for path in dirty if not any(fnmatch.fnmatch(path, pattern) for pattern in generated)
        ]
        if invalid_dirty:
            codes.append("DIRTY_SOURCE")
            details.extend(invalid_dirty)
            checks["working_tree"] = "fail"
        else:
            checks["working_tree"] = "pass"

        branch = _git(repo, "branch", "--show-current")
        if branch != lease.branch_name:
            codes.append("BRANCH_MISMATCH")
            details.append(f"{branch} != {lease.branch_name}")
            checks["branch"] = "fail"
        else:
            checks["branch"] = "pass"

        artifact_hashes: dict[str, str] = {}
        missing: list[str] = []
        for relative in config.get("workspace", {}).get("hash_artifacts", []):
            path = repo / str(relative)
            if path.is_file():
                artifact_hashes[str(relative)] = _sha256(path)
            else:
                missing.append(str(relative))
        for expected in candidate.expected_artifacts:
            path = Path(str(expected))
            if not _looks_like_path(str(expected)):
                continue
            candidates = [path] if path.is_absolute() else [repo / path, Path(lease.artifact_path) / path]
            if not any(candidate_path.exists() for candidate_path in candidates):
                missing.append(str(expected))
        if missing:
            codes.append("EXPECTED_ARTIFACT_MISSING")
            details.extend(missing)
            checks["artifacts"] = "fail"
        else:
            checks["artifacts"] = "pass"

        self._archive_and_restore_generated(repo, Path(lease.artifact_path), dirty, generated)
        verified = not codes
        record.status = "verified" if verified else "provenance_failed"
        return ProvenanceReport(
            execution_id=record.execution_id,
            candidate_id=candidate.candidate_id,
            verified=verified,
            checks=checks,
            failure_codes=list(dict.fromkeys(codes)),
            details=details,
            result_sha=head,
            changed_files=changed,
            commit_count=commit_count,
            artifact_hashes=artifact_hashes,
        )

    @staticmethod
    def _archive_and_restore_generated(
        repo: Path,
        artifact_root: Path,
        dirty: list[str],
        patterns: list[str],
    ) -> None:
        for relative in dirty:
            if not any(fnmatch.fnmatch(relative, pattern) for pattern in patterns):
                continue
            source = repo / relative
            if source.is_file():
                target = artifact_root / "generated" / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            subprocess.run(
                ["git", "-C", str(repo), "restore", "--staged", "--worktree", "--", relative],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )


def _dirty_paths(repo: Path) -> list[str]:
    paths: list[str] = []
    for line in _git(repo, "status", "--porcelain=v1", "--untracked-files=all").splitlines():
        if not line:
            continue
        value = line[3:]
        if " -> " in value:
            value = value.split(" -> ", 1)[1]
        paths.append(value)
    return paths


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _looks_like_path(value: str) -> bool:
    path = Path(value)
    return path.is_absolute() or "/" in value or bool(path.suffix)


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {completed.stderr.strip()}")
    return completed.stdout.strip()


def _git_rc(repo: Path, *args: str) -> int:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    ).returncode
