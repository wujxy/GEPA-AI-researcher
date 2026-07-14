from __future__ import annotations

import os
import subprocess
from contextlib import contextmanager
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Iterator


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

        Candidate edits happen in per-execution sandboxes. The controller checkout
        is only a clean source for materializing those sandboxes, so writes from
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

def _status_path(line: str) -> str:
    # Git porcelain normally uses two status columns plus a space (" M path",
    # "M  path"), but some git versions/configurations can render a one-column
    # index status as "M path". Parse both forms before glob filtering.
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
