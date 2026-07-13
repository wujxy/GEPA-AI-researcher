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
    ApptainerDiscovery,
    ClaudeBind,
    ImageMaterialization,
    Requirements,
    _fingerprint,
    _merge_binds,
    derive_requirements,
    discover_apptainer,
    doctor_runtime,
    ensure_apptainer,
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
        self.assertEqual(req.suggested_base, "docker://alpine:3.20")
        for tool in ("bash", "python", "pytest", "cmake"):
            self.assertIn(tool, req.tools, f"missing detected tool {tool}")
        # Project tools come from host-runtime passthrough, not inferred image packages.
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
        self.assertEqual(req.suggested_base, "docker://alpine:3.20")
        self.assertEqual(req.image_required_tools, ["bash"])

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

    def test_reference_commands_derive_bootstrap_without_runtime_setup(self):
        resolved = {
            "runtime": {"allowed_commands": []},
            "task": {"benchmark_commands": [], "validation_commands": []},
            "contracts": {
                "runtime": {"setup": [], "check": []},
                "reference": {
                    "commands": [
                        "source /cvmfs/juno.ihep.ac.cn/el9_amd64_gcc11/Release/J26.1.1/setup.sh",
                        "cmake -S . -B build -DCMAKE_BUILD_TYPE=Release",
                        "cmake --build build --parallel",
                    ]
                },
            },
        }
        req = derive_requirements(resolved)
        self.assertTrue(req.cvmfs_required)
        self.assertIn("cmake", req.tools)
        self.assertEqual(req.image_required_tools, ["bash"])
        self.assertEqual(resolved["contracts"]["runtime"], {"setup": [], "check": []})

    def test_pytest_validation_uses_host_runtime_not_image_packages(self):
        resolved = {
            "task": {"validation_commands": ["python -m pytest tests/test_consistency.py"]},
            "contracts": {"resources": {"accessible_paths": ["/cvmfs/juno.ihep.ac.cn"]}},
        }

        req = derive_requirements(resolved)

        self.assertIn("pytest", req.tools)
        self.assertEqual(req.image_required_tools, ["bash"])

    def test_host_runtime_binds_existing_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            usr = root / "usr"
            lib64 = root / "lib64"
            usr.mkdir()
            lib64.mkdir()
            binds = ci._host_runtime_readonly_binds({"host_runtime_paths": [str(usr), str(lib64), str(root / "missing")], "bind_host_etc": False})
        self.assertEqual(
            binds,
            [
                {"source": str(usr), "target": str(usr), "mode": "ro"},
                {"source": str(lib64), "target": str(lib64), "mode": "ro"},
            ],
        )

    def test_explicit_bootstrap_tools_are_image_requirements(self):
        resolved = {
            "_runtime_ir": {
                "executor_image": {
                    "bootstrap_tools": ["bash", "which"],
                }
            },
            "runtime": {"allowed_commands": []},
        }
        req = derive_requirements(resolved)
        self.assertIn("which", req.tools)
        self.assertEqual(req.image_required_tools, ["bash", "which"])

    def test_filesystem_paths_are_collected_by_mode(self):
        resolved = {
            "contracts": {
                "resources": {
                    "accessible_paths": ["/cvmfs/juno.ihep.ac.cn"],
                    "writable_paths": ["/scratch/project"],
                }
            },
            "runtime": {"allowed_commands": []},
        }
        req = derive_requirements(resolved)
        self.assertEqual(req.accessible_paths, ["/cvmfs/juno.ihep.ac.cn"])
        self.assertEqual(req.writable_paths, ["/scratch/project"])
        self.assertEqual(req.cvmfs_paths, ["/cvmfs"])

    def test_default_claude_command(self):
        req = derive_requirements({"runtime": {"allowed_commands": []}})
        self.assertEqual(req.claude_command, "claude")


class BuildSifTest(unittest.TestCase):
    def test_build_sif_clears_host_bind_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "image.sif"
            seen = {}

            def fake_run(argv, *, timeout, env=None):
                seen["env"] = env or {}
                tmp_out = Path(argv[2])
                tmp_out.write_text("sif", encoding="utf-8")
                return _completed(0)

            with patch.dict(
                os.environ,
                {
                    "APPTAINER_BIND": "/datafs,/data",
                    "APPTAINER_BINDPATH": "/publicfs",
                    "SINGULARITY_BIND": "/junofs",
                    "SINGULARITY_BINDPATH": "/hpcfs",
                },
                clear=False,
            ), patch.object(ci, "_run", side_effect=fake_run):
                ci._build_sif("docker://alpine:3.20", out)
                self.assertTrue(out.exists())

        for key in ("APPTAINER_BIND", "APPTAINER_BINDPATH", "SINGULARITY_BIND", "SINGULARITY_BINDPATH"):
            self.assertNotIn(key, seen["env"])


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


class DiscoverApptainerTest(unittest.TestCase):
    def _exe(self, root: Path, name: str = "apptainer") -> Path:
        exe = root / name
        exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        exe.chmod(0o755)
        return exe

    def test_configured_executable_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = self._exe(Path(tmp))
            with patch.dict(os.environ, {}, clear=True), patch.object(ci.shutil, "which", return_value=None):
                discovery = discover_apptainer({"executable": str(exe)}, allow_install=False)
        self.assertEqual(discovery.executable, str(exe))
        self.assertEqual(discovery.source, "configured")

    def test_env_executable_is_used_when_unconfigured(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = self._exe(Path(tmp))
            with patch.dict(os.environ, {"GEPA_APPTAINER": str(exe)}, clear=True), \
                 patch.object(ci.shutil, "which", return_value=None):
                discovery = discover_apptainer({}, allow_install=False)
        self.assertEqual(discovery.executable, str(exe))
        self.assertEqual(discovery.source, "env")

    def test_install_hook_can_populate_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cached = root / "apptainer" / "bin" / "apptainer"

            def install(_command):
                cached.parent.mkdir(parents=True)
                cached.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                cached.chmod(0o755)
                return True, None

            with patch.object(ci, "RUNTIME_CACHE_DIR", root), \
                 patch.object(ci, "_install_user_apptainer", side_effect=install), \
                 patch.object(ci.shutil, "which", return_value=None):
                discovery = discover_apptainer({"install_command": "install-apptainer"})
        self.assertEqual(discovery.executable, str(cached))
        self.assertEqual(discovery.source, "cache")
        self.assertTrue(discovery.install_attempted)
        self.assertTrue(discovery.install_ok)

    def test_ensure_apptainer_reports_attempts_when_missing(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(ci.shutil, "which", return_value=None):
            with self.assertRaisesRegex(ci.MaterializationError, "Apptainer runtime was not found") as ctx:
                ensure_apptainer({}, allow_install=False)
        self.assertIn("path:apptainer", str(ctx.exception))




class DoctorRuntimeTest(unittest.TestCase):
    def test_reports_missing_apptainer_without_throwing(self):
        with patch.object(ci, "discover_apptainer", return_value=ApptainerDiscovery(None, None, ["path:apptainer"])), \
             patch.object(ci, "resolve_claude_bind", return_value=ClaudeBind(enabled=False)), \
             patch.object(ci, "_claude_auth_diagnostics", return_value={"ok": False, "host_paths": [], "env_keys": []}):
            report = doctor_runtime({}, agent_command="claude", probe=False)
        self.assertFalse(report["ok"])
        self.assertFalse(report["apptainer"]["ok"])
        self.assertIn("Install Apptainer", report["recommendations"][0])

    def test_can_skip_apptainer_for_local_runtime_config(self):
        with patch.object(ci, "discover_apptainer") as discover, \
             patch.object(ci, "resolve_claude_bind", return_value=ClaudeBind(enabled=True, claude_bin="/bin/claude")), \
             patch.object(ci, "_claude_auth_diagnostics", return_value={"ok": True, "host_paths": ["/home/u/.claude"], "env_keys": []}):
            report = doctor_runtime({}, agent_command="claude", check_apptainer=False)
        self.assertTrue(report["ok"])
        self.assertFalse(discover.called)
        self.assertTrue(report["apptainer"]["ok"])


class ClaudeAuthDiagnosticsTest(unittest.TestCase):
    def test_detects_env_token_without_exposing_value(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "secret"}, clear=True), \
             patch.object(ci.Path, "home", return_value=Path(tempfile.gettempdir()) / "missing-gepa-home"):
            diag = ci._claude_auth_diagnostics()
        self.assertTrue(diag["ok"])
        self.assertEqual(diag["env_keys"], ["ANTHROPIC_API_KEY"])
        self.assertNotIn("secret", str(diag))


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
            "executor": {"runtime_backend": "apptainer"},
            "_runtime_spec": {
                "backend": "apptainer",
                "image": str(sif),
                "apptainer": {"auto_image": False},
            },
        }
        with patch.object(ci, "ensure_apptainer", return_value=ApptainerDiscovery("/usr/bin/apptainer", "test")), \
             patch.object(ci, "_probe_host_runtime", return_value=self._fake_probe()), \
             patch.object(ci, "materialize_executor_image") as mat:
            finalize_runtime(resolved)
            self.assertFalse(mat.called, "materializer must not run when auto_image is False")
        # userns still auto-applied so exec works.
        self.assertTrue(resolved["_runtime_spec"]["apptainer"]["userns"])

    def test_materializes_and_merges_binds_deduped(self):
        resolved = {
            "executor": {"runtime_backend": "apptainer"},
            "_runtime_spec": {
                "backend": "apptainer",
                "image": "docker://alpine:3.20",
                "apptainer": {
                    "readonly_binds": [{"source": "/cvmfs", "target": "/cvmfs", "mode": "ro"}],
                },
            },
        }
        fake_mat = ImageMaterialization(
            sif_path="/cache/abc.sif",
            fingerprint="abc",
            base_image="docker://alpine:3.20",
            requirements=Requirements([], False, [], False, "docker://alpine:3.20", ["bash"], "claude"),
            claude_bind=ClaudeBind(enabled=True, nvm_node_dir="/n", claude_bin="/n/bin/claude", node_bin="/n/bin/node", container_path_prefix="/n/bin"),
            userns=True,
            derived_readonly_binds=[
                {"source": "/cvmfs", "target": "/cvmfs", "mode": "ro"},   # dup of existing
                {"source": "/n", "target": "/n", "mode": "ro"},
            ],
            derived_extra_binds=[
                {"source": "/scratch/project", "target": "/scratch/project", "mode": "rw"},
            ],
        )
        with patch.object(ci, "ensure_apptainer", return_value=ApptainerDiscovery("/usr/bin/apptainer", "test")), \
             patch.object(ci, "_probe_host_runtime", return_value=self._fake_probe()), \
             patch.object(ci, "materialize_executor_image", return_value=fake_mat) as mat:
            finalize_runtime(resolved)
            self.assertTrue(mat.called)
        appt = resolved["_runtime_spec"]["apptainer"]
        self.assertEqual(resolved["_runtime_spec"]["image"], "/cache/abc.sif")
        # /cvmfs deduped, /n appended.
        sources = [(b["source"], b["target"]) for b in appt["readonly_binds"]]
        self.assertEqual(sources, [("/cvmfs", "/cvmfs"), ("/n", "/n")])
        extra_sources = [(b["source"], b["target"], b["mode"]) for b in appt["extra_binds"]]
        self.assertEqual(extra_sources, [("/scratch/project", "/scratch/project", "rw")])
        self.assertEqual(resolved["_runtime_spec"]["command"], "/n/bin/claude")
        self.assertTrue(resolved["_runtime_spec"]["env"]["set"]["PATH"].startswith("/n/bin:"))
        self.assertIn("_materialization", resolved["_meta"])

    def test_userns_not_overridden_when_user_pinned(self):
        resolved = {
            "executor": {"runtime_backend": "apptainer"},
            "_runtime_spec": {
                "backend": "apptainer",
                "image": str(self.cache / "x.sif"),
                "apptainer": {"userns": False},
            },
        }
        with patch.object(ci, "ensure_apptainer", return_value=ApptainerDiscovery("/usr/bin/apptainer", "test")), \
             patch.object(ci, "_probe_host_runtime", return_value=self._fake_probe(userns=True)):
            finalize_runtime(resolved, allow_materialize=False)
        self.assertFalse(resolved["_runtime_spec"]["apptainer"]["userns"])


if __name__ == "__main__":
    unittest.main()
