"""Offline unit tests for gepa_researcher.execution.container_image.

No real apptainer/subprocess is invoked: ``_run`` and ``resolve_command`` are patched.
The real-container path is covered by tests/test_apptainer_real.py (gated).
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import gepa_researcher.execution.container_image as ci
from gepa_researcher.agents.agent_client import CommandResolution
from gepa_researcher.execution.container_image import (
    ClaudeBind,
    ImageMaterialization,
    Requirements,
    _fingerprint,
    _merge_binds,
    derive_requirements,
    finalize_runtime,
    resolve_claude_bind,
)


def _completed(rc=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


class DeriveRequirementsTest(unittest.TestCase):
    def test_omilrec_like_detects_cvmfs_and_build_tools(self):
        resolved = {
            "agent": {"command": "claude"},
            "runtime": {
                "allowed_commands": [
                    "source /cvmfs/juno.ihep.ac.cn/el9_amd64_gcc11/Release/J26.1.1/setup.sh",
                    "bash scripts/quick_bench.sh --evtmax 100",
                ],
                "python_command": "python",
            },
            "task": {
                "benchmark_commands": ["bash scripts/quick_bench.sh --evtmax 100"],
                "validation_commands": [
                    "./build/bin/test_fcn.exe",
                    "python -m pytest tests/test_consistency.py",
                    "cmake --build build",
                ],
            },
            "contracts": {"runtime": {"setup_commands": []}},
        }
        req = derive_requirements(resolved)
        self.assertTrue(req.cvmfs_required)
        self.assertEqual(req.cvmfs_paths, ["/cvmfs"])
        self.assertFalse(req.is_pure_python)
        self.assertEqual(req.suggested_base, "docker://almalinux:9")
        for tool in ("bash", "python", "pytest", "cmake"):
            self.assertIn(tool, req.tools, f"missing detected tool {tool}")
        # Only bash must live in the (thin) image; the rest come from CVMFS.
        self.assertEqual(req.image_required_tools, ["bash"])

    def test_pure_python_project(self):
        resolved = {
            "agent": {"command": "claude"},
            "runtime": {"allowed_commands": ["python3 -m pytest"], "python_command": "python3"},
            "task": {"benchmark_commands": [], "validation_commands": []},
            "contracts": {"runtime": {"setup_commands": []}},
        }
        req = derive_requirements(resolved)
        self.assertFalse(req.cvmfs_required)
        self.assertTrue(req.is_pure_python)
        self.assertEqual(req.suggested_base, "docker://python:3.11-slim")
        self.assertEqual(req.image_required_tools, ["bash", "python3"])

    def test_python3_dedups_bare_python(self):
        resolved = {
            "runtime": {"allowed_commands": ["python3 -m pytest"]},
        }
        req = derive_requirements(resolved)
        self.assertIn("python3", req.tools)
        self.assertNotIn("python", req.tools)

    def test_validation_only_commands_still_derive(self):
        resolved = {
            "runtime": {"allowed_commands": []},
            "task": {"validation_commands": ["python3 -m pytest", "git diff"]},
        }
        req = derive_requirements(resolved)
        self.assertIn("python3", req.tools)
        self.assertIn("git", req.tools)

    def test_default_claude_command(self):
        req = derive_requirements({"runtime": {"allowed_commands": []}})
        self.assertEqual(req.claude_command, "claude")


class FingerprintTest(unittest.TestCase):
    def _bind(self, path: Path):
        return ClaudeBind(enabled=True, nvm_node_dir=str(path.parent.parent),
                          claude_bin=str(path), node_bin=str(path.parent / "node"))

    def test_same_inputs_same_hash(self):
        bind = ClaudeBind(enabled=False)
        self.assertEqual(
            _fingerprint("docker://almalinux:9", ["bash", "git"], bind, []),
            _fingerprint("docker://almalinux:9", ["bash", "git"], bind, []),
        )

    def test_tool_order_independent(self):
        bind = ClaudeBind(enabled=False)
        self.assertEqual(
            _fingerprint("b", ["bash", "git"], bind, []),
            _fingerprint("b", ["git", "bash"], bind, []),
        )

    def test_different_base_changes_hash(self):
        bind = ClaudeBind(enabled=False)
        self.assertNotEqual(
            _fingerprint("docker://almalinux:9", ["bash"], bind, []),
            _fingerprint("docker://python:3.11-slim", ["bash"], bind, []),
        )

    def test_claude_bin_mtime_invalidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "v1.0.0" / "bin"
            bin_dir.mkdir(parents=True)
            claude = bin_dir / "claude"
            claude.write_text("x")
            node = bin_dir / "node"
            node.write_text("x")
            bind = self._bind(claude)
            fp1 = _fingerprint("b", [], bind, [])
            os.utime(claude, (1, 1))
            fp2 = _fingerprint("b", [], bind, [])
            self.assertNotEqual(fp1, fp2)


class ResolveClaudeBindTest(unittest.TestCase):
    def _make_nvm(self, root: Path):
        node_dir = root / "versions" / "node" / "v22.19.0"
        bin_dir = node_dir / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "claude").write_text("x")
        (bin_dir / "node").write_text("x")
        return node_dir, bin_dir

    def test_resolves_nvm_dir_from_claude_bin(self):
        with tempfile.TemporaryDirectory() as tmp:
            node_dir, bin_dir = self._make_nvm(Path(tmp))
            claude = bin_dir / "claude"
            with patch.object(ci, "resolve_command",
                              return_value=CommandResolution([str(claude)])):
                bind = resolve_claude_bind("claude")
            self.assertTrue(bind.enabled)
            self.assertEqual(bind.nvm_node_dir, str(node_dir))
            self.assertEqual(bind.claude_bin, str(claude))
            self.assertEqual(bind.node_bin, str(bin_dir / "node"))

    def test_unresolved_command_disables(self):
        with patch.object(ci, "resolve_command", return_value=None):
            bind = resolve_claude_bind("claude")
        self.assertFalse(bind.enabled)

    def test_missing_node_disables(self):
        with tempfile.TemporaryDirectory() as tmp:
            node_dir, bin_dir = self._make_nvm(Path(tmp))
            (bin_dir / "node").unlink()  # remove node
            claude = bin_dir / "claude"
            with patch.object(ci, "resolve_command",
                              return_value=CommandResolution([str(claude)])):
                bind = resolve_claude_bind("claude")
            self.assertFalse(bind.enabled)


class MergeBindsTest(unittest.TestCase):
    def test_dedupes_by_source_target(self):
        existing = [{"source": "/cvmfs", "target": "/cvmfs", "mode": "ro"}]
        derived = [
            {"source": "/cvmfs", "target": "/cvmfs", "mode": "ro"},  # dup
            {"source": "/n", "target": "/n", "mode": "ro"},          # new
        ]
        merged = _merge_binds(existing, derived)
        self.assertEqual(len(merged), 2)


class FinalizeRuntimeTest(unittest.TestCase):
    def setUp(self):
        # Redirect the image cache to a per-test temp dir.
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cache = Path(self._tmp.name)
        self._cache_patch = patch.object(ci, "CACHE_DIR", self.cache)
        self._cache_patch.start()
        self.addCleanup(self._cache_patch.stop)

    def _fake_probe(self, userns=True):
        return {
            "version": "apptainer version 1.3.3",
            "default_exec_ok": False,
            "userns_exec_ok": True,
            "userns": userns,
            "default_stderr_tail": "",
            "userns_stderr_tail": "",
        }

    def test_skips_when_runtime_backend_local(self):
        resolved = {"executor": {"runtime_backend": "local"}}
        out = finalize_runtime(resolved)
        self.assertNotIn("apptainer", resolved["executor"])

    def test_auto_image_false_with_image_present_does_not_materialize(self):
        sif = self.cache / "existing.sif"
        sif.write_text("img")
        resolved = {
            "executor": {
                "runtime_backend": "apptainer",
                "apptainer": {"image": str(sif), "auto_image": False},
            }
        }
        with patch.object(ci, "_probe_host_runtime", return_value=self._fake_probe()), \
             patch.object(ci, "materialize_executor_image") as mat:
            finalize_runtime(resolved)
            self.assertFalse(mat.called, "materializer must not run when auto_image is False")
        # userns still auto-applied so exec works.
        self.assertTrue(resolved["executor"]["apptainer"]["userns"])

    def test_materializes_and_merges_binds_deduped(self):
        resolved = {
            "executor": {
                "runtime_backend": "apptainer",
                "apptainer": {
                    "readonly_binds": [{"source": "/cvmfs", "target": "/cvmfs", "mode": "ro"}],
                },
            }
        }
        fake_mat = ImageMaterialization(
            sif_path="/cache/abc.sif",
            fingerprint="abc",
            base_image="docker://almalinux:9",
            requirements=Requirements([], False, [], False, "docker://almalinux:9", ["bash"], "claude"),
            claude_bind=ClaudeBind(enabled=True, nvm_node_dir="/n"),
            userns=True,
            derived_readonly_binds=[
                {"source": "/cvmfs", "target": "/cvmfs", "mode": "ro"},   # dup of existing
                {"source": "/n", "target": "/n", "mode": "ro"},
            ],
        )
        with patch.object(ci, "_probe_host_runtime", return_value=self._fake_probe()), \
             patch.object(ci, "materialize_executor_image", return_value=fake_mat) as mat:
            finalize_runtime(resolved)
            self.assertTrue(mat.called)
        appt = resolved["executor"]["apptainer"]
        self.assertEqual(appt["image"], "/cache/abc.sif")
        # /cvmfs deduped, /n appended.
        sources = [(b["source"], b["target"]) for b in appt["readonly_binds"]]
        self.assertEqual(sources, [("/cvmfs", "/cvmfs"), ("/n", "/n")])
        self.assertIn("_materialization", resolved["_meta"])

    def test_userns_not_overridden_when_user_pinned(self):
        resolved = {
            "executor": {
                "runtime_backend": "apptainer",
                "apptainer": {"userns": False, "image": str(self.cache / "x.sif")},
            }
        }
        with patch.object(ci, "_probe_host_runtime", return_value=self._fake_probe(userns=True)):
            finalize_runtime(resolved, allow_materialize=False)
        self.assertFalse(resolved["executor"]["apptainer"]["userns"])


if __name__ == "__main__":
    unittest.main()
