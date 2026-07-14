from __future__ import annotations

import os
import re
import shutil
import subprocess
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from ..domain.execution import ExecutionSpec
from .sandbox import SandboxSession


class MaterializerError(RuntimeError):
    pass


class RepositoryMaterializer:
    def __init__(self, run_dir: Path, workspace_config: dict[str, Any]):
        self.run_dir = Path(run_dir)
        self.config = dict(workspace_config or {})
        self.mode = str(self.config.get("mode", "artifact_directory"))
        self._pre_materialized_lfs_paths = list(self.config.get("pre_materialized_lfs_paths") or [])

    def materialize(self, spec: ExecutionSpec) -> SandboxSession:
        if self.mode != "git_worktree":
            root = self._root()
            repo = root / spec.execution_id / "repo"
            artifacts = root / spec.execution_id / "artifacts"
            scratch = root / spec.execution_id / "scratch"
            for path in (repo, artifacts, scratch):
                path.mkdir(parents=True, exist_ok=True)
            return SandboxSession(
                execution_id=spec.execution_id,
                repo_path=repo,
                artifact_path=artifacts,
                scratch_path=scratch,
                input_revision=spec.input_revision,
                mode="artifact_directory",
                temporary_paths=(repo, artifacts, scratch),
            )

        controller = Path(self.config["repo_path"]).expanduser().resolve()
        if not (controller / ".git").exists():
            raise MaterializerError(f"workspace.repo_path is not a Git working tree: {controller}")
        input_revision = _git(controller, "rev-parse", "--verify", f"{spec.input_revision}^{{commit}}")
        root = self._root()
        base = root / _safe_path(spec.execution_id)
        repo = base / "repo"
        artifacts = base / "artifacts"
        scratch = base / "scratch"
        for path in (base, artifacts, scratch):
            path.mkdir(parents=True, exist_ok=True)

        prefix = str(self.config.get("branch_prefix", "gepa/<run-id>")).replace("<run-id>", self.run_dir.name)
        branch = f"{prefix}/exec/{_safe_ref(spec.execution_id)}"
        if repo.exists():
            actual = _git(repo, "rev-parse", "HEAD")
        else:
            git_env = os.environ.copy()
            git_env["GIT_LFS_SKIP_SMUDGE"] = "1"
            git_env["GIT_TERMINAL_PROMPT"] = "0"
            completed = subprocess.run(
                [
                    "git",
                    "-C",
                    str(controller),
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
                    str(repo),
                    input_revision,
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                env=git_env,
            )
            if completed.returncode != 0:
                raise MaterializerError(f"git worktree add failed: {completed.stderr.strip()}")
            actual = _git(repo, "rev-parse", "HEAD")
        if actual != input_revision:
            raise MaterializerError(f"sandbox start SHA mismatch: expected={input_revision} actual={actual}")

        self._materialize_pre_materialized_lfs_paths(controller, repo)
        return SandboxSession(
            execution_id=spec.execution_id,
            repo_path=repo,
            artifact_path=artifacts,
            scratch_path=scratch,
            input_revision=input_revision,
            mode="git_worktree",
            temporary_paths=(base,),
            controller_repo_path=controller,
            branch_name=branch,
        )

    def assert_clean_for_execution(self, session: SandboxSession) -> dict[str, Any]:
        audit = self.worktree_attribution_audit(session)
        if audit.get("unexpected_dirty"):
            raise MaterializerError(
                "candidate sandbox has unattributed changes before execution: "
                f"path={session.repo_path} status={audit['unexpected_dirty']!r}"
            )
        return audit

    def worktree_attribution_audit(self, session: SandboxSession) -> dict[str, Any]:
        if session.mode != "git_worktree":
            return {"passed": True, "mode": session.mode, "allowed_dirty": [], "unexpected_dirty": []}
        lines = _git(session.repo_path, "status", "--porcelain=v1", "--untracked-files=all").splitlines()
        allowed_globs = [
            *self._pre_materialized_lfs_paths,
            *list(self.config.get("generated_tracked_paths") or []),
            *list(self.config.get("clean_start_ignore_globs") or []),
        ]
        allowed_dirty: list[str] = []
        unexpected_dirty: list[str] = []
        for line in lines:
            if not line:
                continue
            if _status_line_matches_any(line, allowed_globs):
                allowed_dirty.append(line)
            else:
                unexpected_dirty.append(line)
        return {
            "passed": not unexpected_dirty,
            "mode": session.mode,
            "head": _git(session.repo_path, "rev-parse", "HEAD"),
            "allowed_dirty": allowed_dirty,
            "unexpected_dirty": unexpected_dirty,
            "allowed_globs": allowed_globs,
        }

    def cleanup(self, session: SandboxSession) -> None:
        if session.mode == "git_worktree" and session.controller_repo_path and session.repo_path.exists():
            subprocess.run(
                ["git", "-C", str(session.controller_repo_path), "worktree", "remove", "--force", str(session.repo_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
        for path in sorted(session.temporary_paths, key=lambda item: len(item.parts), reverse=True):
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)

    def _root(self) -> Path:
        value = self.config.get("root") or self.run_dir / "sandboxes"
        return Path(str(value).replace("<run-id>", self.run_dir.name)).expanduser().resolve()

    def _materialize_pre_materialized_lfs_paths(self, controller: Path, repo: Path) -> None:
        for pattern in self._pre_materialized_lfs_paths:
            rel_pattern = Path(str(pattern))
            if rel_pattern.is_absolute() or ".." in rel_pattern.parts:
                raise MaterializerError(f"pre_materialized_lfs_paths entry must be repo-relative: {pattern}")
            matches = sorted(controller.glob(str(pattern)))
            if not matches:
                raise MaterializerError(f"pre_materialized_lfs_paths entry matched no files: {pattern}")
            repo_root = repo.resolve()
            for source in matches:
                rel = source.relative_to(controller)
                target = (repo / rel).resolve()
                if not (target == repo_root or repo_root in target.parents):
                    raise MaterializerError(f"materialized target escapes sandbox: {rel}")
                target.parent.mkdir(parents=True, exist_ok=True)
                if source.is_dir():
                    shutil.copytree(source, target, dirs_exist_ok=True, symlinks=True)
                else:
                    shutil.copy2(source, target, follow_symlinks=False)


def _safe_ref(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-") or "execution"


def _safe_path(value: str) -> str:
    return _safe_ref(value)


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


def _status_line_matches_any(line: str, patterns: list[str]) -> bool:
    path = _status_path(line)
    return any(_matches_glob(path, pattern) for pattern in patterns)


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise MaterializerError(f"git {' '.join(args)} failed: {completed.stderr.strip()}")
    return completed.stdout.strip()
