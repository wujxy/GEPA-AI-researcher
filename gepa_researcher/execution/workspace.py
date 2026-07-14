from __future__ import annotations

import os
import re
import shutil
import subprocess
from contextlib import contextmanager
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Iterator

from ..models.schemas import Candidate, WorkspaceLease


class WorkspaceError(RuntimeError):
    pass


class WorkspaceManager:
    def __init__(self, run_dir: Path, config: dict[str, Any]):
        self.run_dir = run_dir
        self.config = dict(config.get("workspace") or {})
        self.mode = str(self.config.get("mode", "artifact_directory"))
        self._controller_ignore_globs = [
            ".cache/**",
            ".clangd/**",
            ".claude/**",
            ".vscode/**",
            ".gepa-running.lock",
            "judgement.json",
            *list(self.config.get("controller_ignore_globs") or []),
        ]
        guard_config = self.config.get("controller_guard") or {}
        self._write_protect_controller = bool(guard_config.get("write_protect", True))
        self._pre_materialized_lfs_paths = list(self.config.get("pre_materialized_lfs_paths") or [])

    def prepare(self, candidate: Candidate, parent_sha: str = "") -> WorkspaceLease:
        if self.mode != "git_worktree":
            path = self.run_dir / "agent_work" / f"round_{candidate.round_id:03d}" / candidate.candidate_id
            path.mkdir(parents=True, exist_ok=True)
            return WorkspaceLease(
                candidate_id=candidate.candidate_id,
                round_id=candidate.round_id,
                requested_parent_sha=parent_sha,
                actual_start_sha=parent_sha,
                branch_name="",
                worktree_path=str(path),
                artifact_path=str(path),
                mode="artifact_directory",
            )

        repo = Path(self.config["repo_path"]).expanduser().resolve()
        if not (repo / ".git").exists():
            raise WorkspaceError(f"workspace.repo_path is not a Git working tree: {repo}")
        requested = parent_sha or str(self.config.get("baseline_ref", ""))
        if not requested:
            raise WorkspaceError("git_worktree mode requires a parent SHA or workspace.baseline_ref")
        start_sha = _git(repo, "rev-parse", "--verify", f"{requested}^{{commit}}")
        root_value = self.config.get("root") or self.run_dir / "worktrees"
        root = Path(str(root_value).replace("<run-id>", self.run_dir.name)).expanduser().resolve()
        worktree = root / f"round_{candidate.round_id:03d}" / candidate.candidate_id / "repo"
        artifacts = root / f"round_{candidate.round_id:03d}" / candidate.candidate_id / "artifacts"
        prefix = str(self.config.get("branch_prefix", "gepa/<run-id>")).replace("<run-id>", self.run_dir.name)
        branch = f"{prefix}/round-{candidate.round_id:03d}/{_safe_ref(candidate.candidate_id)}"
        worktree.parent.mkdir(parents=True, exist_ok=True)
        artifacts.mkdir(parents=True, exist_ok=True)

        if worktree.exists():
            actual = _git(worktree, "rev-parse", "HEAD")
        else:
            git_env = os.environ.copy()
            git_env["GIT_LFS_SKIP_SMUDGE"] = "1"
            git_env["GIT_TERMINAL_PROMPT"] = "0"
            completed = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "-c",
                    "filter.lfs.required=false",
                    "-c",
                    "filter.lfs.smudge=",
                    "-c",
                    "filter.lfs.process=",
                    "worktree",
                    "add",
                    "-B",
                    branch,
                    str(worktree),
                    start_sha,
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                env=git_env,
            )
            if completed.returncode != 0:
                raise WorkspaceError(f"git worktree add failed: {completed.stderr.strip()}")
            actual = _git(worktree, "rev-parse", "HEAD")
        if actual != start_sha:
            raise WorkspaceError(f"worktree start SHA mismatch: expected={start_sha} actual={actual}")

        self._materialize_pre_materialized_lfs_paths(repo, worktree)

        # Worktree is ready - runtime backends handle any task-specific mounts.
        return WorkspaceLease(
            candidate_id=candidate.candidate_id,
            round_id=candidate.round_id,
            requested_parent_sha=start_sha,
            actual_start_sha=actual,
            branch_name=branch,
            worktree_path=str(worktree),
            artifact_path=str(artifacts),
            mode="git_worktree",
        )

    def controller_snapshot(self) -> dict[str, str] | None:
        if self.mode != "git_worktree":
            return None
        repo = Path(self.config["repo_path"]).expanduser().resolve()
        return {
            "head": _git(repo, "rev-parse", "HEAD"),
            "status": self._filtered_status(repo),
        }

    def assert_controller_unchanged(self, snapshot: dict[str, str] | None) -> None:
        if snapshot is None:
            return
        current = self.controller_snapshot()
        if current != snapshot:
            raise WorkspaceError(
                "controller repository changed during candidate execution: "
                f"before={snapshot} after={current}"
            )

    @contextmanager
    def protect_controller(self) -> Iterator[None]:
        """Mark and optionally write-protect the controller checkout while GEPA runs.

        Candidate edits happen in per-candidate worktrees. The controller checkout
        is only a clean source for materializing those worktrees, so writes from
        editor indexers or language servers are noise that can invalidate the run.
        The .git directory is intentionally left writable because git worktree
        operations update shared metadata there.
        """
        if self.mode != "git_worktree":
            yield
            return
        repo = Path(self.config["repo_path"]).expanduser().resolve()
        lock_path = repo / ".gepa-running.lock"
        previous_modes: dict[Path, int] = {}
        try:
            self._recover_stale_controller_protection(repo, lock_path)
            self._write_controller_lock(lock_path)
            if self._write_protect_controller:
                previous_modes = self._remove_worktree_write_bits(repo)
            yield
        finally:
            if previous_modes:
                self._restore_modes(previous_modes)
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass

    def worktree_snapshot(self, worktree_path: str) -> dict[str, str]:
        """Capture worktree state snapshot for integrity validation.

        Args:
            worktree_path: Path to the worktree directory

        Returns:
            Dictionary with 'head' (commit SHA) and 'status' (git status output)
            Returns empty dict if not in git_worktree mode or worktree doesn't exist
        """
        if self.mode != "git_worktree":
            return {}
        worktree = Path(worktree_path).expanduser().resolve()
        if not (worktree / ".git").exists():
            return {}
        try:
            return {
                "head": _git(worktree, "rev-parse", "HEAD"),
                "status": _git(worktree, "status", "--porcelain=v1", "--untracked-files=all"),
            }
        except WorkspaceError:
            # Worktree exists but git commands fail (e.g., corrupted .git)
            return {"error": "git_command_failed", "path": str(worktree)}

    def assert_worktree_unchanged(self, snapshot: dict[str, str], worktree_path: str) -> None:
        """Assert worktree has not been modified during execution.

        Args:
            snapshot: Previous worktree snapshot from worktree_snapshot()
            worktree_path: Path to the worktree directory

        Raises:
            WorkspaceError: If worktree state has changed (excluding untracked files)
        """
        if not snapshot or "error" in snapshot:
            # Skip validation if snapshot was empty or had errors
            return
        current = self.worktree_snapshot(worktree_path)
        if not current:
            return  # Skip if can't get current state
        if current != snapshot:
            raise WorkspaceError(
                f"Worktree corrupted during execution: "
                f"path={worktree_path} before={snapshot} after={current}"
            )

    def assert_worktree_clean_for_execution(self, worktree_path: str) -> None:
        """Require a candidate worktree to start from a clean, attributable state."""
        if self.mode != "git_worktree":
            return
        worktree = Path(worktree_path).expanduser().resolve()
        status = self._filtered_worktree_status(worktree)
        if status:
            raise WorkspaceError(
                "candidate worktree is not clean before execution: "
                f"path={worktree} status={status!r}"
            )

    def _filtered_worktree_status(self, worktree: Path) -> str:
        lines = _git(worktree, "status", "--porcelain=v1", "--untracked-files=all").splitlines()
        ignore_globs = [
            *self._controller_ignore_globs,
            *self._pre_materialized_lfs_paths,
            *list(self.config.get("clean_start_ignore_globs") or []),
        ]
        kept = [line for line in lines if line and not self._status_line_matches_any(line, ignore_globs)]
        return "\n".join(kept)

    def _status_line_matches_any(self, line: str, patterns: list[str]) -> bool:
        path = _status_path(line)
        return any(_matches_glob(path, pattern) for pattern in patterns)

    def _filtered_status(self, repo: Path) -> str:
        lines = _git(repo, "status", "--porcelain=v1", "--untracked-files=all").splitlines()
        kept = [line for line in lines if line and not self._is_ignored_status_line(line)]
        return "\n".join(kept)

    def _is_ignored_status_line(self, line: str) -> bool:
        return self._status_line_matches_any(line, self._controller_ignore_globs)

    def _recover_stale_controller_protection(self, repo: Path, lock_path: Path) -> None:
        lock_pid = self._read_lock_pid(lock_path)
        if lock_pid is not None and _pid_is_alive(lock_pid):
            raise WorkspaceError(
                "controller repository is locked by an active GEPA run: "
                f"pid={lock_pid} lock={lock_path}"
            )
        if lock_path.exists() or not os.access(repo, os.W_OK):
            self._add_owner_write_bits(repo)
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _read_lock_pid(self, lock_path: Path) -> int | None:
        try:
            for line in lock_path.read_text(encoding="utf-8").splitlines():
                if line.startswith("pid="):
                    return int(line.split("=", 1)[1])
        except (OSError, ValueError):
            return None
        return None

    def _write_controller_lock(self, lock_path: Path) -> None:
        lock_path.write_text(
            "\n".join(
                [
                    "GEPA controller checkout is in use.",
                    f"pid={os.getpid()}",
                    f"run_dir={self.run_dir}",
                    "Do not edit this checkout while the run is active.",
                    "",
                ]
            ),
            encoding="utf-8",
        )

    def _add_owner_write_bits(self, repo: Path) -> None:
        for path in self._controller_paths_to_protect(repo):
            try:
                mode = path.stat().st_mode
                path.chmod(mode | 0o200)
            except OSError:
                pass

    def _remove_worktree_write_bits(self, repo: Path) -> dict[Path, int]:
        previous_modes: dict[Path, int] = {}
        for path in self._controller_paths_to_protect(repo):
            try:
                mode = path.stat().st_mode
            except OSError:
                continue
            new_mode = mode & ~0o222
            if new_mode != mode:
                previous_modes[path] = mode
                try:
                    path.chmod(new_mode)
                except OSError:
                    previous_modes.pop(path, None)
        return previous_modes

    def _controller_paths_to_protect(self, repo: Path) -> list[Path]:
        paths: set[Path] = {repo, repo / ".gepa-running.lock"}
        for rel in _git(repo, "ls-files").splitlines():
            tracked = repo / rel
            paths.add(tracked)
            paths.update(parent for parent in tracked.parents if repo in (parent, *parent.parents))
        for rel in [".cache", ".clangd", ".claude", ".vscode", "judgement.json"]:
            path = repo / rel
            if not path.exists():
                continue
            paths.add(path)
            if path.is_dir():
                paths.update(child for child in path.rglob("*"))
        return sorted(paths, key=lambda path: len(path.parts), reverse=True)

    def _restore_modes(self, previous_modes: dict[Path, int]) -> None:
        for path, mode in sorted(previous_modes.items(), key=lambda item: len(item[0].parts)):
            try:
                path.chmod(mode)
            except OSError:
                pass

    def _materialize_pre_materialized_lfs_paths(self, repo: Path, worktree: Path) -> None:
        for pattern in self._pre_materialized_lfs_paths:
            rel_pattern = Path(str(pattern))
            if rel_pattern.is_absolute() or ".." in rel_pattern.parts:
                raise WorkspaceError(f"pre_materialized_lfs_paths entry must be repo-relative: {pattern}")
            matches = sorted(repo.glob(str(pattern)))
            if not matches:
                raise WorkspaceError(f"pre_materialized_lfs_paths entry matched no files: {pattern}")
            worktree_root = worktree.resolve()
            for source in matches:
                rel = source.relative_to(repo)
                target = (worktree / rel).resolve()
                if not (target == worktree_root or worktree_root in target.parents):
                    raise WorkspaceError(f"materialized target escapes worktree: {rel}")
                target.parent.mkdir(parents=True, exist_ok=True)
                if source.is_dir():
                    shutil.copytree(source, target, dirs_exist_ok=True, symlinks=True)
                else:
                    shutil.copy2(source, target, follow_symlinks=False)


def _safe_ref(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-") or "candidate"


def _status_path(line: str) -> str:
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


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise WorkspaceError(f"git {' '.join(args)} failed: {completed.stderr.strip()}")
    return completed.stdout.strip()
