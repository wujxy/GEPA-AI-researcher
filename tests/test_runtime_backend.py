import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gepa_researcher.runtime_backend import ApptainerRuntimeBackend, RuntimeBackendError, runtime_backend_for
from gepa_researcher.schemas import Candidate, ExecutionRecord, WorkspaceLease


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
            config = {
                "agent": {"command": "host-claude"},
                "workspace": {"readonly_assets": [{"source": str(readonly), "target": "TEMP/fixtures/time_pdf.bin"}]},
                "executor": {
                    "runtime_backend": "apptainer",
                    "apptainer": {
                        "image": str(image),
                        "executable": str(apptainer),
                        "command": "claude-in-container",
                        "claude_home_template": str(home_template),
                        "env_allowlist": ["GEPA_ALLOW_TEST"],
                        "readonly_binds": [{"source": str(readonly), "target": "/readonly/fixture.bin"}],
                        "extra_binds": [{"source": str(root), "target": "/extra", "mode": "rw"}],
                    },
                },
            }

            with patch.dict(os.environ, {"GEPA_ALLOW_TEST": "allowed", "SECRET_TEST": "hidden"}, clear=False):
                runtime = ApptainerRuntimeBackend(root, config).prepare(_candidate(), lease, _record())

            self.assertEqual(runtime.backend, "apptainer")
            self.assertEqual(runtime.repo_path, "/workspace/repo")
            self.assertEqual(runtime.artifact_path, "/workspace/artifacts")
            self.assertFalse(runtime.inherit_host_env)
            self.assertEqual(runtime.command, "claude-in-container")
            self.assertEqual(runtime.env["GEPA_WORKTREE"], "/workspace/repo")
            self.assertEqual(runtime.env["GEPA_ARTIFACTS"], "/workspace/artifacts")
            self.assertEqual(runtime.env["HOME"], "/workspace/home")
            self.assertEqual(runtime.env["TMPDIR"], "/workspace/scratch/tmp")
            self.assertEqual(runtime.env["GEPA_ALLOW_TEST"], "allowed")
            self.assertNotIn("SECRET_TEST", runtime.env)
            self.assertTrue((Path(runtime.artifacts["host_home"]) / "auth.json").exists())
            self.assertTrue((Path(runtime.artifacts["host_scratch"]) / "tmp").is_dir())
            joined = "\n".join(runtime.command_prefix)
            self.assertIn("--cleanenv", runtime.command_prefix)
            self.assertIn("--containall", runtime.command_prefix)
            self.assertIn(f"{readonly}:/workspace/repo/TEMP/fixtures/time_pdf.bin:ro", joined)
            self.assertIn(f"{readonly}:/readonly/fixture.bin:ro", joined)
            self.assertIn(f"{root}:/extra:rw", joined)

    def test_apptainer_backend_fails_without_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            apptainer = root / "apptainer"
            apptainer.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            os.chmod(apptainer, 0o755)
            config = {
                "executor": {
                    "runtime_backend": "apptainer",
                    "apptainer": {"image": str(root / "missing.sif"), "executable": str(apptainer)},
                }
            }

            with self.assertRaisesRegex(RuntimeBackendError, "image does not exist"):
                ApptainerRuntimeBackend(root, config).prepare(_candidate(), _lease(root), _record())


if __name__ == "__main__":
    unittest.main()
