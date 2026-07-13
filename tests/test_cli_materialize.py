from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gepa_researcher.cli import _resolve, _should_materialize


class CliMaterializePolicyTest(unittest.TestCase):
    def test_run_and_validate_materialize_by_default(self):
        self.assertTrue(_should_materialize(argparse.Namespace(command="run", no_materialize=False)))
        self.assertTrue(_should_materialize(argparse.Namespace(command="validate", no_materialize=False)))

    def test_no_materialize_disables_run_and_validate_materialization(self):
        self.assertFalse(_should_materialize(argparse.Namespace(command="run", no_materialize=True)))
        self.assertFalse(_should_materialize(argparse.Namespace(command="validate", no_materialize=True)))

    def test_inspection_commands_materialize_only_when_requested(self):
        self.assertFalse(_should_materialize(argparse.Namespace(command="resolve", materialize=False)))
        self.assertTrue(_should_materialize(argparse.Namespace(command="resolve", materialize=True)))
        self.assertFalse(_should_materialize(argparse.Namespace(command="explain", materialize=False)))
        self.assertTrue(_should_materialize(argparse.Namespace(command="explain", materialize=True)))

    def test_run_resolve_materializes_apptainer_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "task.yaml"
            config_path.write_text("kind: task\n", encoding="utf-8")
            resolved = {"executor": {"runtime_backend": "apptainer"}, "_runtime_spec": {"backend": "apptainer"}}
            materialized = {"executor": {"runtime_backend": "apptainer"}, "_runtime_spec": {"backend": "apptainer", "image": "/cache/gepa.sif"}}
            args = argparse.Namespace(command="run", config=str(config_path), run_dir=None, resume=False, no_materialize=False)
            with patch("gepa_researcher.cli.load_and_resolve", return_value=resolved), \
                 patch("gepa_researcher.cli.finalize_runtime", return_value=materialized) as finalize:
                _, out = _resolve(args)
            finalize.assert_called_once_with(resolved, allow_materialize=True)
            self.assertEqual(out["_runtime_spec"]["image"], "/cache/gepa.sif")

    def test_no_materialize_skips_finalize_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "task.yaml"
            config_path.write_text("kind: task\n", encoding="utf-8")
            resolved = {"executor": {"runtime_backend": "apptainer"}, "_runtime_spec": {"backend": "apptainer"}}
            args = argparse.Namespace(command="run", config=str(config_path), run_dir=None, resume=False, no_materialize=True)
            with patch("gepa_researcher.cli.load_and_resolve", return_value=resolved), \
                 patch("gepa_researcher.cli.finalize_runtime") as finalize:
                _, out = _resolve(args)
            finalize.assert_not_called()
            self.assertIs(out, resolved)


if __name__ == "__main__":
    unittest.main()
