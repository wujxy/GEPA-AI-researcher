from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

from .schemas import Candidate, WorkspaceLease


class WorkspaceError(RuntimeError):
    pass


class WorkspaceManager:
    def __init__(self, run_dir: Path, config: dict[str, Any]):
        self.run_dir = run_dir
        self.config = dict(config.get("workspace") or {})
        self.mode = str(self.config.get("mode", "artifact_directory"))

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
            completed = subprocess.run(
                ["git", "-C", str(repo), "worktree", "add", "-b", branch, str(worktree), start_sha],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if completed.returncode != 0:
                raise WorkspaceError(f"git worktree add failed: {completed.stderr.strip()}")
            actual = _git(worktree, "rev-parse", "HEAD")
        if actual != start_sha:
            raise WorkspaceError(f"worktree start SHA mismatch: expected={start_sha} actual={actual}")

        # Worktree is ready - let executor create task-specific directories as needed
        # This makes GEPA framework task-agnostic and avoids OMILREC/JUNO specific hardcoded paths

        self._map_readonly_assets(worktree)
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
            "status": _git(repo, "status", "--porcelain=v1", "--untracked-files=all"),
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

    def _map_readonly_assets(self, worktree: Path) -> None:
        for item in self.config.get("readonly_assets", []):
            source = Path(str(item["source"])).expanduser().resolve()
            target = worktree / str(item["target"])
            if not source.exists():
                raise WorkspaceError(f"readonly asset does not exist: {source}")
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() or target.is_symlink():
                continue
            os.symlink(source, target)


def _safe_ref(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-") or "candidate"


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
