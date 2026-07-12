from __future__ import annotations

import fnmatch
import subprocess
from pathlib import Path

from .schemas import CommitAudit


def audit_commit(
    repo: Path,
    parent_sha: str,
    frozen_globs: list[str] | None = None,
) -> CommitAudit:
    """Record what the executor's commit actually changed, and guard frozen paths.

    This replaces the former ``ProvenanceVerifier``. There is no ``verified``
    verdict and no working-tree inspection:

    - build / pytest / benchmark debris left in the worktree is NOT a signal;
    - whether the candidate *worked* (build ok, metric present, validation
      passed) is the judger's call, via the trace's metrics/validation fields;
    - workspace invariants (branch, base SHA, ancestry) are the workspace
      manager's responsibility -- it created the worktree from ``parent_sha``.

    The ONE hard guard retained here is that the delivered commit must not touch
    a frozen path (e.g. immutable fixtures): that is a silent-corruption risk the
    judger cannot detect from metrics alone. Everything else the old verifier
    checked was either a workspace invariant or redundant with the judger, and
    each had become a source of false candidate-killing failures.

    Read-only: this never mutates the worktree.
    """
    frozen_globs = frozen_globs or []
    head = _git(repo, "rev-parse", "HEAD")
    changed = [
        line for line in _git(repo, "diff", "--name-only", parent_sha, head).splitlines() if line
    ]
    commit_count = int(_git(repo, "rev-list", "--count", f"{parent_sha}..{head}") or 0)
    frozen_violations = sorted({path for path in changed if _matches_any(path, frozen_globs)})
    return CommitAudit(
        result_sha=head,
        changed_files=changed,
        commit_count=commit_count,
        frozen_violations=frozen_violations,
    )


def _matches_any(path: str, patterns: list[str]) -> bool:
    # Mirrors admission._matches_any so frozen-glob matching is consistent
    # across the proposal gate and the commit audit.
    return any(
        fnmatch.fnmatch(path, pattern)
        or ("**/" in pattern and fnmatch.fnmatch(path, pattern.replace("**/", "")))
        for pattern in patterns
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
        raise RuntimeError(f"git {' '.join(args)} failed: {completed.stderr.strip()}")
    return completed.stdout.strip()
