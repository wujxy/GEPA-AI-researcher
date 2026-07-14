"""REAL Apptainer end-to-end tests.

Gated on GEPA_REAL_APPTAINER=1 (and apptainer being present) so the default offline
suite never pulls images or starts containers. When enabled, these tests build a
tiny real SIF and drive the REAL ApptainerRuntimeBackend command prefix — the path
the fake-apptainer tests in test_apptainer_integration.py cannot cover.

Run with:
    GEPA_REAL_APPTAINER=1 python -m unittest tests.test_apptainer_real
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gepa_researcher.domain.execution import CapabilityPolicy, ExecutionBudget, ExecutionPhase, ExecutionRecord, ExecutionSpec
from gepa_researcher.execution.container_image import MaterializationError, _build_sif
from gepa_researcher.execution.runtime_backend import ApptainerRuntimeBackend
from gepa_researcher.execution.sandbox import SandboxSession

_REAL = bool(os.environ.get("GEPA_REAL_APPTAINER"))
_APPTAINER_AVAILABLE = _REAL and (subprocess.run(["which", "apptainer"]).returncode == 0)
_SKIP = not (_REAL and _APPTAINER_AVAILABLE)

_BASE = "docker://alpine:3.20"


def _spec(execution_id: str) -> ExecutionSpec:
    return ExecutionSpec(
        execution_id=execution_id,
        run_id="run",
        candidate_id="cand_000",
        round_id=0,
        phase=ExecutionPhase.IMPLEMENTATION,
        input_revision="a" * 40,
        dataset_ref=None,
        evaluator_version=None,
        budget=ExecutionBudget(wall_seconds=600),
        capability_policy=CapabilityPolicy(repo_writable=True),
    )


def _record(execution_id: str) -> ExecutionRecord:
    return ExecutionRecord.from_spec(_spec(execution_id))


def _session(root: Path, execution_id: str) -> SandboxSession:
    repo = root / "repo"
    artifacts = root / "artifacts"
    scratch = root / f"scratch-{execution_id}"
    repo.mkdir(parents=True, exist_ok=True)
    artifacts.mkdir(parents=True, exist_ok=True)
    scratch.mkdir(parents=True, exist_ok=True)
    return SandboxSession(
        execution_id=execution_id,
        repo_path=repo,
        artifact_path=artifacts,
        scratch_path=scratch,
        input_revision="a" * 40,
        mode="git_worktree",
        temporary_paths=(repo, artifacts, scratch),
    )


@unittest.skipIf(_SKIP, "Set GEPA_REAL_APPTAINER=1 with apptainer installed to run")
class RealApptainerTest(unittest.TestCase):
    """Exercises the real ApptainerRuntimeBackend prefix against a real SIF."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.sif = Path(cls._tmp.name) / "real.sif"
        try:
            _build_sif(_BASE, cls.sif, timeout=600)
        except MaterializationError as exc:
            raise unittest.SkipTest(f"could not build real SIF (network?): {exc}") from exc

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _config(self, root: Path):
        return {
            "executor": {
                "runtime_backend": "apptainer",
                "apptainer": {
                    "image": str(self.sif),
                    "executable": "apptainer",
                    "command": "/bin/sh",
                    "cleanenv": True,
                    "containall": True,
                    "writable_tmpfs": True,
                    "userns": True,
                },
            }
        }

    def _run_prefix(self, runtime, script: str) -> str:
        cmd = list(runtime.command_prefix) + ["/bin/sh", "-c", script]
        env = dict(os.environ)
        env["GEPA_HOST_SECRET"] = "should-not-leak"
        result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(result.returncode, 0, f"container run failed:\n{result.stderr}")
        return result.stdout

    def test_userns_flag_present_in_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = ApptainerRuntimeBackend(root, self._config(root))
            runtime = backend.prepare(_spec("exec-1"), _session(root, "exec-1"), _record("exec-1"))
            self.assertIn("--userns", runtime.command_prefix)
            self.assertIn("--home", runtime.command_prefix)

    def test_artifact_writeback_and_cleanenv_isolation_and_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = ApptainerRuntimeBackend(root, self._config(root))
            runtime = backend.prepare(_spec("exec-1"), _session(root, "exec-1"), _record("exec-1"))
            out = self._run_prefix(
                runtime,
                "echo HOME=$HOME; "
                "echo SECRET=${GEPA_HOST_SECRET:-none}; "
                "echo from-container > /workspace/artifacts/proof.txt",
            )
            # HOME set via --home (not the host home).
            self.assertIn("HOME=/workspace/artifacts/home_exec-1", out)
            # cleanenv: host-only secret must NOT leak into the container.
            self.assertIn("SECRET=none", out)
            # artifact written from inside appears on the host.
            proof = Path(runtime.artifacts["host_artifacts"]) / "proof.txt"
            self.assertTrue(proof.exists(), "artifact write-back failed")
            self.assertIn("from-container", proof.read_text())

    def test_per_execution_scratch_uniqueness(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = ApptainerRuntimeBackend(root, self._config(root))
            r1 = backend.prepare(_spec("exec-1"), _session(root, "exec-1"), _record("exec-1"))
            r2 = backend.prepare(_spec("exec-2"), _session(root, "exec-2"), _record("exec-2"))
            self.assertNotEqual(r1.artifacts["host_scratch"], r2.artifacts["host_scratch"])
            self.assertNotEqual(r1.artifacts["host_home"], r2.artifacts["host_home"])
            self.assertTrue((Path(r1.artifacts["host_scratch"]) / "tmp").is_dir())
            self.assertTrue((Path(r2.artifacts["host_scratch"]) / "tmp").is_dir())


@unittest.skipIf(_SKIP, "Set GEPA_REAL_APPTAINER=1 with apptainer installed to run")
class RealMaterializationTest(unittest.TestCase):
    """Drives materialize_executor_image end-to-end (real build + cache reuse)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._cache_patch = patch(
            "gepa_researcher.execution.container_image.CACHE_DIR", Path(self._tmp.name)
        )
        self._cache_patch.start()
        self.addCleanup(self._cache_patch.stop)

    def test_materialize_builds_then_reuses_cache(self):
        import gepa_researcher.execution.container_image as ci

        resolved = {
            "agent": {"command": "claude"},
            "executor": {
                "runtime_backend": "apptainer",
                "apptainer": {"executable": "apptainer"},
            },
            "runtime": {"allowed_commands": ["python3 -m pytest"], "python_command": "python3"},
            "task": {"benchmark_commands": [], "validation_commands": []},
            "contracts": {"runtime": {"setup_commands": []}},
        }
        # Pure-python -> docker://python:3.11-slim (has bash + python3).
        host_probe = {
            "version": "real", "default_exec_ok": False, "userns_exec_ok": True,
            "userns": True, "default_stderr_tail": "", "userns_stderr_tail": "",
        }
        with patch.object(ci, "_probe_host_runtime", return_value=host_probe):
            mat1 = ci.materialize_executor_image(resolved, host_probe=host_probe)
            self.assertTrue(Path(mat1.sif_path).exists())
            self.assertFalse(mat1.diagnostics.get("cache_hit"))
            mat2 = ci.materialize_executor_image(resolved, host_probe=host_probe)
            self.assertEqual(mat1.sif_path, mat2.sif_path)
            self.assertTrue(mat2.diagnostics.get("cache_hit"))


if __name__ == "__main__":
    unittest.main()
