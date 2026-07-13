import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gepa_researcher.execution.runtime_backend import ApptainerRuntimeBackend, RuntimeBackendError, RuntimeLease, runtime_backend_for
from gepa_researcher.models.schemas import Candidate, ExecutionRecord, WorkspaceLease


def _candidate():
    return Candidate(
        candidate_id="cand_000",
        round_id=0,
        hypothesis="h",
        scope="s",
        proposed_change="c",
        rationale="r",
        expected_improvement="e",
        risk="risk",
        prompt_text="",
        created_at="now",
    )


def _record():
    return ExecutionRecord(
        execution_id="exec-1",
        candidate_id="cand_000",
        round_id=0,
        parent_candidate_id=None,
        requested_parent_sha="parent-sha",
        actual_start_sha="parent-sha",
        result_sha=None,
        branch_name="branch",
        worktree_path="",
    )


def _lease(root: Path):
    repo = root / "repo"
    artifacts = root / "artifacts"
    repo.mkdir()
    artifacts.mkdir()
    return WorkspaceLease(
        candidate_id="cand_000",
        round_id=0,
        requested_parent_sha="parent-sha",
        actual_start_sha="parent-sha",
        branch_name="branch",
        worktree_path=str(repo),
        artifact_path=str(artifacts),
        mode="git_worktree",
    )


def _apptainer_config(root: Path, image: Path, apptainer: Path, readonly: Path | None = None, **apptainer_overrides):
    mounts = []
    if readonly is not None:
        mounts.append({
            "source": str(readonly),
            "target": "/workspace/repo/TEMP/fixtures/time_pdf.bin",
            "mode": "ro",
        })
    apptainer_cfg = {
        "executable": str(apptainer),
        "cleanenv": False,
        "containall": False,
        "writable_tmpfs": True,
        "userns": True,
        "auto_init_claude_home": True,
    }
    apptainer_cfg.update(apptainer_overrides)
    return {
        "agent": {"command": "host-claude"},
        "executor": {
            "runtime_backend": "apptainer",
            # Stale legacy values should be ignored by ApptainerRuntimeBackend.
            "apptainer": {"command": "legacy-command", "env_allowlist": ["SECRET_TEST"]},
        },
        "_runtime_spec": {
            "backend": "apptainer",
            "image": str(image),
            "workdir": "/workspace/repo",
            "command": "claude-in-container",
            "env": {"pass": ["GEPA_ALLOW_TEST"], "set": {"STATIC_ENV": "1"}},
            "setup": [
                {"op": "source", "path": "/cvmfs/juno/setup.sh", "required": True},
                {"op": "source", "path": "InstallArea/setup.sh", "base": "workdir", "required": False},
            ],
            "check": [{"name": "check-1", "command": "which gcc", "required": True}],
            "mounts": mounts,
            "tools": ["bash"],
            "apptainer": apptainer_cfg,
        },
    }


class RuntimeBackendTest(unittest.TestCase):
    def test_local_backend_preserves_host_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lease = _lease(root)
            runtime = runtime_backend_for({"executor": {}}, root).prepare(_candidate(), lease, _record())

            self.assertEqual(runtime.backend, "local")
            self.assertEqual(runtime.repo_path, lease.worktree_path)
            self.assertEqual(runtime.artifact_path, lease.artifact_path)
            self.assertTrue(runtime.inherit_host_env)
            self.assertEqual(runtime.env["GEPA_WORKTREE"], lease.worktree_path)

    def test_apptainer_backend_builds_isolated_runtime_lease(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "executor.sif"
            image.write_text("image", encoding="utf-8")
            apptainer = root / "apptainer"
            apptainer.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            os.chmod(apptainer, 0o755)
            home_template = root / "claude-home-template"
            home_template.mkdir()
            (home_template / "auth.json").write_text("{}", encoding="utf-8")
            readonly = root / "fixture.bin"
            readonly.write_text("fixture", encoding="utf-8")
            lease = _lease(root)
            config = _apptainer_config(
                root, image, apptainer, readonly,
                claude_home_template=str(home_template),
                readonly_binds=[{"source": str(readonly), "target": "/readonly/fixture.bin"}],
                extra_binds=[{"source": str(root), "target": "/extra", "mode": "rw"}],
            )

            with patch.dict(os.environ, {"GEPA_ALLOW_TEST": "allowed", "SECRET_TEST": "hidden"}, clear=False):
                runtime = ApptainerRuntimeBackend(root, config).prepare(_candidate(), lease, _record())

            self.assertEqual(runtime.backend, "apptainer")
            self.assertEqual(runtime.repo_path, "/workspace/repo")
            self.assertEqual(runtime.artifact_path, "/workspace/artifacts")
            self.assertFalse(runtime.inherit_host_env)
            self.assertEqual(runtime.command, "claude-in-container")
            self.assertEqual(runtime.artifacts["executor_command"], "claude-in-container")
            self.assertEqual(runtime.env, {})
            self.assertTrue((Path(runtime.artifacts["host_home"]) / "auth.json").exists())
            self.assertTrue((Path(runtime.artifacts["host_scratch"]) / "tmp").is_dir())
            joined = "\n".join(runtime.command_prefix)
            self.assertNotIn("--cleanenv", runtime.command_prefix)
            self.assertNotIn("--containall", runtime.command_prefix)
            self.assertIn("--env", runtime.command_prefix)
            self.assertIn("GEPA_WORKTREE=/workspace/repo", runtime.command_prefix)
            self.assertIn("GEPA_ARTIFACTS=/workspace/artifacts", runtime.command_prefix)
            self.assertIn("STATIC_ENV=1", runtime.command_prefix)
            self.assertIn("GEPA_ALLOW_TEST=allowed", runtime.command_prefix)
            self.assertNotIn("SECRET_TEST", joined)
            self.assertIn("--home", runtime.command_prefix)
            home_idx = runtime.command_prefix.index("--home")
            self.assertTrue(
                runtime.command_prefix[home_idx + 1].endswith(":/workspace/artifacts/home_exec-1"),
                runtime.command_prefix[home_idx + 1],
            )
            self.assertNotIn("HOME=/workspace/artifacts/home_exec-1", runtime.command_prefix)
            self.assertIn("TMPDIR=/workspace/artifacts/scratch_exec-1/tmp", runtime.command_prefix)
            self.assertIn(f"{readonly}:/workspace/repo/TEMP/fixtures/time_pdf.bin:ro", joined)
            self.assertIn(f"{readonly}:/readonly/fixture.bin:ro", joined)
            self.assertIn(f"{root}:/extra:rw", joined)
            self.assertIn("/usr/bin/env", runtime.command_prefix)
            self.assertIn("bash", runtime.command_prefix)
            self.assertNotIn("-lc", runtime.command_prefix)
            self.assertIn('exec "$@"', runtime.artifacts["runtime_shell"])
            self.assertNotIn("host_launcher", runtime.artifacts)
            entrypoint = Path(runtime.artifacts["runtime_entrypoint_host"])
            self.assertTrue(entrypoint.exists())
            self.assertEqual(runtime.command_prefix[-3:], ["/usr/bin/env", "bash", runtime.artifacts["runtime_entrypoint_container"]])

    def test_apptainer_backend_redacts_environment_values_in_to_dict(self):
        """Security: Verify environment variable values are redacted in to_dict()"""
        runtime_lease = RuntimeLease(
            backend="apptainer",
            repo_path="/workspace/repo",
            artifact_path="/workspace/artifacts",
            host_cwd="/host/repo",
            command="claude",
            command_prefix=["apptainer", "exec"],
            env={
                "SECRET_KEY": "super_secret_value_12345",
                "API_TOKEN": "api_token_xyz",
                "GEPA_CANDIDATE_ID": "cand_000"
            },
            inherit_host_env=False,
        )

        # Convert to dict (as happens when persisting to trace artifacts)
        lease_dict = runtime_lease.to_dict()

        # Verify environment values are redacted but keys are preserved
        self.assertIn("env", lease_dict)
        self.assertTrue(lease_dict["env"].get("_redacted"))
        self.assertEqual(lease_dict["env"]["_count"], 3)
        self.assertIn("SECRET_KEY", lease_dict["env"]["_keys"])
        self.assertIn("API_TOKEN", lease_dict["env"]["_keys"])
        self.assertIn("GEPA_CANDIDATE_ID", lease_dict["env"]["_keys"])

        # Verify actual secret values are NOT in the dict
        dict_string = str(lease_dict)
        self.assertNotIn("super_secret_value_12345", dict_string)
        self.assertNotIn("api_token_xyz", dict_string)

    def test_local_backend_redacts_environment_values_in_to_dict(self):
        """Security: Verify local backend also redacts environment values"""
        runtime_lease = RuntimeLease(
            backend="local",
            repo_path="/local/repo",
            artifact_path="/local/artifacts",
            host_cwd="/local/repo",
            env={
                "PASSWORD": "my_password",
                "USERNAME": "admin"
            },
            inherit_host_env=True,
        )

        lease_dict = runtime_lease.to_dict()

        # Verify redaction
        self.assertTrue(lease_dict["env"].get("_redacted"))
        self.assertEqual(lease_dict["env"]["_count"], 2)
        self.assertNotIn("my_password", str(lease_dict))

    def test_workspace_manager_worktree_snapshot_and_validation(self):
        """Test worktree integrity validation methods"""
        from gepa_researcher.execution.workspace import WorkspaceManager, WorkspaceError
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "test_repo"
            repo.mkdir()

            # Initialize a git repository
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True, capture_output=True)

            # Create initial commit
            (repo / "test.txt").write_text("content", encoding="utf-8")
            subprocess.run(["git", "add", "test.txt"], cwd=repo, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)

            # Create WorkspaceManager in git_worktree mode
            config = {
                "workspace": {
                    "mode": "git_worktree",
                    "repo_path": str(repo),
                    "baseline_ref": "HEAD",
                }
            }
            wm = WorkspaceManager(root / "run_dir", config)

            # Test worktree_snapshot
            snapshot = wm.worktree_snapshot(str(repo))
            self.assertIn("head", snapshot)
            self.assertIn("status", snapshot)
            self.assertNotIn("error", snapshot)

            # Test assert_worktree_unchanged with no changes
            wm.assert_worktree_unchanged(snapshot, str(repo))  # Should not raise

            # Modify the worktree
            (repo / "modified.txt").write_text("modified", encoding="utf-8")

            # Get new snapshot
            modified_snapshot = wm.worktree_snapshot(str(repo))

            # Test assert_worktree_unchanged with changes (should raise)
            with self.assertRaises(WorkspaceError) as ctx:
                wm.assert_worktree_unchanged(snapshot, str(repo))
            self.assertIn("Worktree corrupted during execution", str(ctx.exception))

    def test_workspace_manager_worktree_snapshot_handles_errors_gracefully(self):
        """Test worktree_snapshot handles non-git directories gracefully"""
        from gepa_researcher.execution.workspace import WorkspaceManager

        with tempfile.TemporaryDirectory() as tmp:
            # Test with non-git directory
            non_git_dir = Path(tmp) / "not_a_repo"
            non_git_dir.mkdir()

            config = {"workspace": {"mode": "artifact_directory"}}
            wm = WorkspaceManager(Path(tmp) / "run_dir", config)

            # Should return empty dict for non-git directories
            snapshot = wm.worktree_snapshot(str(non_git_dir))
            self.assertEqual(snapshot, {})

            # Should not raise error with empty snapshot
            wm.assert_worktree_unchanged({}, str(non_git_dir))

    def test_apptainer_backend_injects_userns_and_extra_exec_args(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "executor.sif"
            image.write_text("image", encoding="utf-8")
            apptainer = root / "apptainer"
            apptainer.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            os.chmod(apptainer, 0o755)
            lease = _lease(root)
            config = _apptainer_config(root, image, apptainer, userns=True, extra_exec_args=["--no-mount", "home"])
            runtime = ApptainerRuntimeBackend(root, config).prepare(_candidate(), lease, _record())
            self.assertIn("--userns", runtime.command_prefix)
            self.assertIn("--no-mount", runtime.command_prefix)
            self.assertIn("home", runtime.command_prefix)


    def test_apptainer_backend_uses_runtime_spec_entrypoint_wrapper(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "executor.sif"
            image.write_text("image", encoding="utf-8")
            apptainer = root / "apptainer"
            apptainer.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            os.chmod(apptainer, 0o755)
            lease = _lease(root)
            config = _apptainer_config(root, image, apptainer)

            runtime = ApptainerRuntimeBackend(root, config).prepare(_candidate(), lease, _record())

            self.assertEqual(runtime.command, "claude-in-container")
            self.assertEqual(runtime.command_prefix[-3:], ["/usr/bin/env", "bash", runtime.artifacts["runtime_entrypoint_container"]])
            self.assertNotIn("-lc", runtime.command_prefix)
            entrypoint = Path(runtime.artifacts["runtime_entrypoint_host"])
            self.assertTrue(entrypoint.exists())
            self.assertEqual(entrypoint.read_text(encoding="utf-8"), runtime.artifacts["runtime_shell"] + "\n")
            shell = runtime.artifacts["runtime_shell"]
            self.assertIn("cd /workspace/repo", shell)
            self.assertIn("source /cvmfs/juno/setup.sh", shell)
            self.assertIn("if [ -f InstallArea/setup.sh ]; then source InstallArea/setup.sh", shell)
            self.assertIn("which gcc", shell)
            self.assertIn('exec "$@"', shell)


    def test_apptainer_backend_accepts_docker_uri_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            apptainer = root / "apptainer"
            apptainer.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            os.chmod(apptainer, 0o755)
            config = _apptainer_config(root, root / "unused.sif", apptainer)
            config["_runtime_spec"]["image"] = "docker://almalinux:9"

            runtime = ApptainerRuntimeBackend(root, config).prepare(_candidate(), _lease(root), _record())

            self.assertIn("docker://almalinux:9", runtime.command_prefix)

    def test_apptainer_backend_binds_git_common_dir_for_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "executor.sif"
            image.write_text("image", encoding="utf-8")
            apptainer = root / "apptainer"
            apptainer.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            os.chmod(apptainer, 0o755)
            lease = _lease(root)
            worktree = Path(lease.worktree_path)
            common_git = root / "source" / ".git"
            gitdir = common_git / "worktrees" / "repo1"
            gitdir.mkdir(parents=True)
            (gitdir / "commondir").write_text("../..\n", encoding="utf-8")
            (worktree / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
            config = _apptainer_config(root, image, apptainer)

            runtime = ApptainerRuntimeBackend(root, config).prepare(_candidate(), lease, _record())

            self.assertIn(f"{common_git}:{common_git}:rw", runtime.command_prefix)

    def test_apptainer_backend_dedupes_binds_by_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "executor.sif"
            image.write_text("image", encoding="utf-8")
            apptainer = root / "apptainer"
            apptainer.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            os.chmod(apptainer, 0o755)
            readonly = root / "fixture.bin"
            readonly.write_text("fixture", encoding="utf-8")
            target = "/workspace/repo/TEMP/fixtures/time_pdf.bin"
            config = _apptainer_config(
                root,
                image,
                apptainer,
                readonly=readonly,
                readonly_binds=[{"source": str(readonly), "target": target, "mode": "ro"}],
            )

            runtime = ApptainerRuntimeBackend(root, config).prepare(_candidate(), _lease(root), _record())

            bind_values = [
                runtime.command_prefix[index + 1]
                for index, item in enumerate(runtime.command_prefix[:-1])
                if item == "--bind"
            ]
            self.assertEqual(sum(1 for value in bind_values if f":{target}:" in value), 1)

    def test_apptainer_backend_fails_without_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            apptainer = root / "apptainer"
            apptainer.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            os.chmod(apptainer, 0o755)
            config = _apptainer_config(root, root / "missing.sif", apptainer)

            with self.assertRaisesRegex(RuntimeBackendError, "isolation.image resolved path does not exist"):
                ApptainerRuntimeBackend(root, config).prepare(_candidate(), _lease(root), _record())


if __name__ == "__main__":
    unittest.main()
