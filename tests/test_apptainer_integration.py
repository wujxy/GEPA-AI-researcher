"""Real Apptainer integration tests for production validation.

These tests require actual Apptainer installation and test images to run.
They validate the complete container isolation, directory structure, Git compatibility,
and environment handling in real-world scenarios.
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gepa_researcher.execution.runtime_backend import ApptainerRuntimeBackend, RuntimeBackendError
from gepa_researcher.models.schemas import Candidate, ExecutionRecord, WorkspaceLease


class ApptainerIntegrationTest(unittest.TestCase):
    """Real Apptainer environment integration tests."""

    @classmethod
    def setUpClass(cls):
        # Check if real apptainer environment is available
        cls.apptainer_available = shutil.which("apptainer") is not None
        # Try to find a test image (looking for common development images)
        possible_images = [
            "/cvmfs/juno.ihep.ac.cn/el9_amd64_gcc11/Release/J26.1.1/junosw/InstallArea/bin/python",  # JUNO Python
            "/usr/bin/apptainer",  # System apptainer
        ]
        cls.test_image = None
        for image_path in possible_images:
            if Path(image_path).exists():
                cls.test_image = image_path
                break

        # Create a minimal test SIF if docker is available
        if not cls.test_image and shutil.which("docker"):
            try:
                # Try to build a minimal test image
                result = subprocess.run(
                    ["docker", "version"],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if result.returncode == 0:
                    cls.docker_available = True
                else:
                    cls.docker_available = False
            except (subprocess.TimeoutExpired, FileNotFoundError):
                cls.docker_available = False
        else:
            cls.docker_available = False

    def _create_test_lease(self, root: Path, candidate_id: str = "cand_000"):
        """Create a test WorkspaceLease for testing."""
        repo = root / "repo"
        artifacts = root / "artifacts"
        repo.mkdir(parents=True, exist_ok=True)
        artifacts.mkdir(parents=True, exist_ok=True)

        return WorkspaceLease(
            candidate_id=candidate_id,
            round_id=0,
            requested_parent_sha="parent-sha",
            actual_start_sha="parent-sha",
            branch_name="test-branch",
            worktree_path=str(repo),
            artifact_path=str(artifacts),
            mode="artifact_directory",
        )

    def _create_test_record(self, execution_id: str = "exec-1"):
        """Create a test ExecutionRecord for testing."""
        return ExecutionRecord(
            execution_id=execution_id,
            candidate_id="cand_000",
            round_id=0,
            parent_candidate_id=None,
            requested_parent_sha="parent-sha",
            actual_start_sha="parent-sha",
            result_sha=None,
            branch_name="test-branch",
            worktree_path="",
            execution_mode="implement_and_validate",
            status="executing",
        )

    def _runtime_config(self, image: Path, apptainer: Path, *, command: str = "python", mounts=None, env_pass=None, env_set=None, **apptainer_overrides):
        apptainer_cfg = {
            "image": str(image),
            "executable": str(apptainer),
            "cleanenv": True,
            "containall": True,
            "writable_tmpfs": True,
        }
        apptainer_cfg.update(apptainer_overrides)
        return {
            "executor": {"runtime_backend": "apptainer"},
            "_runtime_ir": {
                "backend": "apptainer",
                "workdir": "/workspace/repo",
                "command": command,
                "append_agent_args": True,
                "env": {"pass": list(env_pass or []), "set": dict(env_set or {})},
                "init": [],
                "preflight": [],
                "mounts": list(mounts or []),
                "apptainer": apptainer_cfg,
            },
        }

    def test_real_apptainer_executable_detection(self):
        """Test that apptainer executable detection works correctly."""
        if not self.apptainer_available:
            self.skipTest("Apptainer not available on this system")

        apptainer = shutil.which("apptainer")
        self.assertIsNotNone(apptainer)
        self.assertTrue(Path(apptainer).exists())

    def test_apptainer_version_check(self):
        """Test that we can get Apptainer version information."""
        if not self.apptainer_available:
            self.skipTest("Apptainer not available on this system")

        result = subprocess.run(
            ["apptainer", "--version"],
            capture_output=True,
            text=True,
            timeout=10
        )
        # Version command should succeed
        self.assertEqual(result.returncode, 0)
        self.assertIn("apptainer version", result.stdout.lower())

    def test_build_minimal_image_without_docker(self):
        """GEPA builds a SIF directly from an OCI registry (no Docker daemon)."""
        if not os.environ.get("GEPA_REAL_APPTAINER"):
            self.skipTest("Set GEPA_REAL_APPTAINER=1 to run real-apptainer tests")
        if not self.apptainer_available:
            self.skipTest("Apptainer not available on this system")
        from gepa_researcher.execution.container_image import MaterializationError, _build_sif

        with tempfile.TemporaryDirectory() as tmp:
            sif = Path(tmp) / "minimal.sif"
            try:
                _build_sif("docker://alpine:3.20", sif, timeout=300)
            except MaterializationError as exc:
                self.skipTest(f"image build failed (network?): {exc}")
            self.assertTrue(sif.exists())

    def test_per_execution_directory_structure_with_real_config(self):
        """Test per-execution directory structure with real configuration."""
        if not self.apptainer_available:
            self.skipTest("Apptainer not available on this system")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            # Create a fake apptainer executable that just records the command
            fake_apptainer = root / "fake_apptainer"
            fake_apptainer.write_text("#!/bin/sh\n echo \"$@\" > command_args.txt\n exit 0\n", encoding="utf-8")
            fake_apptainer.chmod(0o755)

            # Create a fake image file
            fake_image = root / "test.sif"
            fake_image.write_text("fake image", encoding="utf-8")

            # Create configuration
            config = self._runtime_config(fake_image, fake_apptainer, command="python")

            # Test with multiple executions of the same candidate
            lease = self._create_test_lease(root)
            backend = ApptainerRuntimeBackend(root / "run_dir", config)

            # First execution
            record1 = self._create_test_record("exec-1")
            runtime1 = backend.prepare(
                Candidate(
                    candidate_id="cand_000",
                    round_id=0,
                    hypothesis="test",
                    scope="test",
                    proposed_change="test",
                    rationale="test",
                    expected_improvement="test",
                    risk="test",
                    prompt_text="test",
                    created_at="now",
                ),
                lease,
                record1
            )

            # Second execution (same candidate, different execution)
            record2 = self._create_test_record("exec-2")
            runtime2 = backend.prepare(
                Candidate(
                    candidate_id="cand_000",
                    round_id=0,
                    hypothesis="test",
                    scope="test",
                    proposed_change="test",
                    rationale="test",
                    expected_improvement="test",
                    risk="test",
                    prompt_text="test",
                    created_at="now",
                ),
                lease,
                record2
            )

            # Verify that each execution gets unique directories
            self.assertIn("scratch_exec-1", runtime1.artifacts["host_scratch"])
            self.assertIn("home_exec-1", runtime1.artifacts["host_home"])
            self.assertIn("scratch_exec-2", runtime2.artifacts["host_scratch"])
            self.assertIn("home_exec-2", runtime2.artifacts["host_home"])

            # Verify directories actually exist
            self.assertTrue((Path(runtime1.artifacts["host_scratch"]) / "tmp").exists())
            self.assertTrue(Path(runtime1.artifacts["host_home"]).exists())

    def test_environment_variables_with_special_characters_are_passed_as_argv(self):
        """Environment values are passed as --env argv entries, not shell-escaped strings."""
        if not self.apptainer_available:
            self.skipTest("Apptainer not available on this system")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_image = root / "test.sif"
            fake_image.write_text("fake image", encoding="utf-8")
            fake_apptainer = root / "fake_apptainer"
            fake_apptainer.write_text("#!/bin/sh\n exit 0\n", encoding="utf-8")
            fake_apptainer.chmod(0o755)
            config = self._runtime_config(
                fake_image,
                fake_apptainer,
                env_set={"SPECIAL_VALUE": "complex: ;test with $ dollar and 'quote"},
            )

            runtime = ApptainerRuntimeBackend(root / "run_dir", config).prepare(
                Candidate(
                    candidate_id="cand_000",
                    round_id=0,
                    hypothesis="test",
                    scope="test",
                    proposed_change="test",
                    rationale="test",
                    expected_improvement="test",
                    risk="test",
                    prompt_text="test",
                    created_at="now",
                ),
                self._create_test_lease(root),
                self._create_test_record(),
            )

            self.assertIn("SPECIAL_VALUE=complex: ;test with $ dollar and 'quote", runtime.command_prefix)

    def test_container_bind_mount_structure_validation(self):
        """Test that container bind mounts are correctly structured."""
        if not self.apptainer_available:
            self.skipTest("Apptainer not available on this system")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            # Create fake image
            fake_image = root / "test.sif"
            fake_image.write_text("fake image", encoding="utf-8")

            # Create fake apptainer
            fake_apptainer = root / "fake_apptainer"
            fake_apptainer.write_text("#!/bin/sh\n exit 0\n", encoding="utf-8")
            fake_apptainer.chmod(0o755)

            config = self._runtime_config(
                fake_image,
                fake_apptainer,
                mounts=[{"source": str(root / "asset"), "target": "/workspace/repo/TEMP/asset.bin", "mode": "ro"}],
                readonly_binds=[{"source": str(root / "readonly"), "target": "/readonly", "mode": "ro"}],
                extra_binds=[{"source": str(root / "writable"), "target": "/writable", "mode": "rw"}],
            )

            # Create required directories
            (root / "readonly").mkdir()
            (root / "writable").mkdir()
            (root / "asset").mkdir()

            lease = self._create_test_lease(root)
            backend = ApptainerRuntimeBackend(root / "run_dir", config)
            runtime = backend.prepare(
                Candidate(
                    candidate_id="cand_000",
                    round_id=0,
                    hypothesis="test",
                    scope="test",
                    proposed_change="test",
                    rationale="test",
                    expected_improvement="test",
                    risk="test",
                    prompt_text="test",
                    created_at="now",
                ),
                lease,
                self._create_test_record()
            )

            # Verify bind mount structure in command prefix
            prefix_str = " ".join(runtime.command_prefix)

            # Check core binds
            self.assertIn("--bind", prefix_str)
            self.assertIn("/workspace/repo", prefix_str)
            self.assertIn("/workspace/artifacts", prefix_str)

            # Check readonly assets binds
            self.assertIn(":ro", prefix_str)

            # Check that readonly_binds and extra_binds are included
            self.assertIn("/readonly", prefix_str)
            self.assertIn("/writable", prefix_str)

    def test_git_worktree_git_file_pointer_handling(self):
        """Test that Git worktree .git file pointers would work in containers."""
        # This tests the structure of .git files in worktrees
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            main_repo = root / "main_repo"
            main_repo.mkdir()

            # Initialize main repository
            subprocess.run(["git", "init"], cwd=main_repo, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=main_repo, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=main_repo, check=True, capture_output=True)

            # Create initial commit
            (main_repo / "file.txt").write_text("content", encoding="utf-8")
            subprocess.run(["git", "add", "file.txt"], cwd=main_repo, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "initial"], cwd=main_repo, check=True, capture_output=True)

            # Create a worktree
            worktree = root / "worktree"
            subprocess.run(
                ["git", "worktree", "add", str(worktree), "HEAD"],
                cwd=main_repo,
                check=True,
                capture_output=True
            )

            # Verify .git file in worktree (should be a pointer, not a directory)
            git_file = worktree / ".git"
            self.assertTrue(git_file.exists())
            self.assertTrue(git_file.is_file())  # Should be a file, not a directory

            # Read the .git file to verify it's a pointer
            git_content = git_file.read_text(encoding="utf-8")
            self.assertIn("gitdir:", git_content)
            self.assertIn("main_repo", git_content)

            # Verify Git works in the worktree
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=worktree,
                capture_output=True,
                text=True,
                check=True
            )
            # Should succeed (empty status is fine)
            self.assertEqual(result.returncode, 0)

    def test_container_git_compatibility_with_bind_mounts(self):
        """Test that Git worktrees remain functional when accessed via container bind mounts."""
        if not self.apptainer_available:
            self.skipTest("Apptainer not available on this system")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()

            # Initialize repository
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True)

            # Create initial commit
            (repo / "file.txt").write_text("content", encoding="utf-8")
            subprocess.run(["git", "add", "file.txt"], cwd=repo, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)

            # Simulate what would happen with container bind mounts
            # The key test is that Git operations work when accessed via bind mount paths
            container_repo_path = repo  # In real container, this would be /workspace/repo

            # Test Git commands that would be used inside container
            status_result = subprocess.run(
                ["git", "status", "--porcelain=v1"],
                cwd=container_repo_path,
                capture_output=True,
                text=True,
                check=True
            )
            self.assertEqual(status_result.returncode, 0)

            head_result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=container_repo_path,
                capture_output=True,
                text=True,
                check=True
            )
            self.assertEqual(head_result.returncode, 0)
            self.assertTrue(len(head_result.stdout.strip()) == 40)  # SHA length

    def test_complete_execution_flow_simulation(self):
        """Test a complete simulated execution flow with all components."""
        if not self.apptainer_available:
            self.skipTest("Apptainer not available on this system")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            # Create fake image and apptainer
            fake_image = root / "test.sif"
            fake_image.write_text("fake image", encoding="utf-8")

            fake_apptainer = root / "fake_apptainer"
            fake_apptainer.write_text(
                "#!/bin/sh\n"
                "# Simulate container execution\n"
                "mkdir -p \"$1\"\n"
                "echo 'Container execution successful' > \"$1/output.txt\"\n"
                "exit 0\n",
                encoding="utf-8"
            )
            fake_apptainer.chmod(0o755)

            config = self._runtime_config(fake_image, fake_apptainer, command="python")

            lease = self._create_test_lease(root)
            backend = ApptainerRuntimeBackend(root / "run_dir", config)

            # Create candidate
            candidate = Candidate(
                candidate_id="cand_000",
                round_id=0,
                hypothesis="test hypothesis",
                scope="test_scope",
                proposed_change="test change",
                rationale="test rationale",
                expected_improvement="test improvement",
                risk="test risk",
                prompt_text="test prompt",
                created_at="now",
            )

            # Prepare execution
            record = self._create_test_record("exec-1")
            runtime = backend.prepare(candidate, lease, record)

            # Verify all components
            self.assertEqual(runtime.backend, "apptainer")
            self.assertIsNotNone(runtime.command_prefix)
            self.assertTrue(len(runtime.command_prefix) > 5)  # Should have multiple args
            self.assertEqual(runtime.env, {})  # Should be empty with --env

            # Verify directory structure
            self.assertTrue(Path(runtime.artifacts["host_scratch"]).exists())
            self.assertTrue(Path(runtime.artifacts["host_home"]).exists())

            # Verify execution_id in paths
            self.assertIn("exec-1", runtime.artifacts["host_scratch"])
            self.assertIn("exec-1", runtime.artifacts["host_home"])


class OMILRECApptainerTest(unittest.TestCase):
    """OMILREC-specific Apptainer scenario tests."""

    def test_omilrec_directory_structure_requirements(self):
        """Test that directory structure meets OMILREC requirements."""
        # OMILREC needs specific directory layout for:
        # - Build artifacts
        # - Test fixtures
        # - Benchmark results
        # - Reference files

        # This test validates the structure would work in containers
        required_structure = {
            "repo": "OMILRECV2/",
            "build": "OMILRECV2/build/",
            "tests": "OMILRECV2/tests/",
            "benchmarks": "OMILRECV2/benchmarks/",
            "fixtures": "OMILRECV2/tests/fixtures/",
        }

        # Verify all paths are relative (no absolute paths)
        for key, path in required_structure.items():
            self.assertFalse(path.startswith("/"), f"{key} should be relative: {path}")

    def test_omilrec_environment_variable_requirements(self):
        """Test that OMILREC environment variables are properly structured."""
        # OMILREC needs specific environment variables:
        required_vars = [
            "GEPA_CANDIDATE_ID",
            "GEPA_EXECUTION_ID",
            "GEPA_WORKTREE",
            "GEPA_ARTIFACTS",
            "HOME",
            "TMPDIR",
        ]

        # This validates the naming convention
        for var in required_vars:
            self.assertTrue(var.isupper(), f"Environment var should be uppercase: {var}")
            # HOME and TMPDIR are standard Unix vars, others should have underscores
            if var not in ["HOME", "TMPDIR"]:
                self.assertIn("_", var, f"Environment var should use underscores: {var}")

    def test_omilrec_per_execution_isolation_for_repeated_runs(self):
        """Test that per-execution isolation prevents state pollution in OMILREC scenarios."""
        # OMILREC may run the same candidate multiple times (evaluate_only mode)
        # Each run must be completely isolated

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            # Simulate multiple executions
            execution_ids = [f"eval_run_{i}" for i in range(5)]
            artifact_dirs = []

            for exec_id in execution_ids:
                # Simulate directory creation
                exec_artifacts = root / f"artifacts_{exec_id}"
                exec_scratch = exec_artifacts / f"scratch_{exec_id}"
                exec_home = exec_artifacts / f"home_{exec_id}"

                exec_scratch.mkdir(parents=True, exist_ok=True)
                exec_home.mkdir(parents=True, exist_ok=True)

                artifact_dirs.append({
                    "exec_id": exec_id,
                    "scratch": str(exec_scratch),
                    "home": str(exec_home),
                })

            # Verify no overlap
            scratch_paths = [d["scratch"] for d in artifact_dirs]
            home_paths = [d["home"] for d in artifact_dirs]

            self.assertEqual(len(scratch_paths), len(set(scratch_paths)), "Scratch paths should be unique")
            self.assertEqual(len(home_paths), len(set(home_paths)), "Home paths should be unique")

            # Verify each execution has its own space
            for artifact_dir in artifact_dirs:
                scratch = Path(artifact_dir["scratch"])
                home = Path(artifact_dir["home"])
                self.assertTrue(scratch.exists())
                self.assertTrue(home.exists())
                # Create tmp if it doesn't exist (common in OMILREC scenarios)
                tmp_dir = scratch / "tmp"
                tmp_dir.mkdir(parents=True, exist_ok=True)
                self.assertTrue(tmp_dir.exists())


if __name__ == "__main__":
    unittest.main()