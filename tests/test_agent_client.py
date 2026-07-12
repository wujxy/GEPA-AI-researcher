import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from gepa_researcher.agent_client import AgentError, ClaudeCodeClient


class ClaudeCodeClientTest(unittest.TestCase):
    def test_run_json_prints_child_stderr_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "fake_claude.py"
            script.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env python3",
                        "import sys",
                        "print('child progress line', file=sys.stderr, flush=True)",
                        "print('{\"ok\": true}', flush=True)",
                    ]
                ),
                encoding="utf-8",
            )
            os.chmod(script, 0o755)

            client = ClaudeCodeClient(command=str(script), timeout_seconds=5)
            output = StringIO()

            with redirect_stdout(output):
                result = client.run_json("hello", label="fake")

            self.assertEqual(result.data, {"ok": True})
            text = output.getvalue()
            self.assertIn("fake Claude call started", text)
            self.assertIn("child progress line", text)
            self.assertIn("fake Claude call finished", text)

    def test_resolves_claude_from_nvm_when_path_does_not_include_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            nvm_dir = Path(tmp) / ".nvm"
            claude = nvm_dir / "versions" / "node" / "v24.11.1" / "bin" / "claude"
            claude.parent.mkdir(parents=True)
            claude.write_text(
                "\n".join(
                    [
                        f"#!{sys.executable}",
                        "print('{\"ok\": true}', flush=True)",
                    ]
                ),
                encoding="utf-8",
            )
            os.chmod(claude, 0o755)

            client = ClaudeCodeClient(command="claude", timeout_seconds=5)
            with patch.dict(os.environ, {"PATH": "", "NVM_DIR": str(nvm_dir)}, clear=False):
                output = StringIO()
                with redirect_stdout(output):
                    result = client.run_json("hello", label="fake")

            self.assertEqual(result.data, {"ok": True})
            self.assertIn(str(claude), output.getvalue())

    def test_resolves_claude_from_nvm_global_package_native_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            nvm_dir = Path(tmp) / ".nvm"
            node = nvm_dir / "versions" / "node" / "v24.11.1" / "bin" / "node"
            wrapper = (
                nvm_dir
                / "versions"
                / "node"
                / "v24.11.1"
                / "lib"
                / "node_modules"
                / "@anthropic-ai"
                / "claude-code"
                / "cli-wrapper.cjs"
            )
            native_claude = (
                nvm_dir
                / "versions"
                / "node"
                / "v24.11.1"
                / "lib"
                / "node_modules"
                / "@anthropic-ai"
                / "claude-code"
                / "node_modules"
                / "@anthropic-ai"
                / "claude-code-linux-x64"
                / "claude"
            )
            node.parent.mkdir(parents=True)
            wrapper.parent.mkdir(parents=True)
            native_claude.parent.mkdir(parents=True)
            node.write_text(
                "\n".join(
                    [
                        f"#!{sys.executable}",
                        "print('{\"ok\": true}', flush=True)",
                    ]
                ),
                encoding="utf-8",
            )
            wrapper.write_text("wrapper placeholder", encoding="utf-8")
            native_claude.write_text(
                "\n".join(
                    [
                        f"#!{sys.executable}",
                        "raise SystemExit('native binary should not be called directly')",
                    ]
                ),
                encoding="utf-8",
            )
            os.chmod(node, 0o755)
            os.chmod(native_claude, 0o755)

            client = ClaudeCodeClient(command="claude", timeout_seconds=5)
            with patch.dict(os.environ, {"PATH": "", "NVM_DIR": str(nvm_dir)}, clear=False):
                output = StringIO()
                with redirect_stdout(output):
                    result = client.run_json("hello", label="fake")

            self.assertEqual(result.data, {"ok": True})
            text = output.getvalue()
            self.assertIn(str(node), text)
            self.assertIn(str(wrapper), text)

    def test_run_json_attaches_raw_output_on_non_json(self):
        # When the agent emits prose with no JSON, run_json must raise AgentError
        # AND surface the raw text via .raw_output so the executor repair path can
        # quote it back.
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "fake_claude.py"
            script.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env python3",
                        "print('That is just the run-1 header. Waiting for the actual ms/evt results.')",
                    ]
                ),
                encoding="utf-8",
            )
            os.chmod(script, 0o755)
            client = ClaudeCodeClient(command=str(script), timeout_seconds=5)
            output = StringIO()
            with redirect_stdout(output):
                with self.assertRaises(AgentError) as ctx:
                    client.run_json("hello", label="fake")
            self.assertIn(
                "Waiting for the actual ms/evt results.",
                getattr(ctx.exception, "raw_output", None),
            )


if __name__ == "__main__":
    unittest.main()
