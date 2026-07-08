from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


class AgentError(RuntimeError):
    pass


@dataclass
class AgentResult:
    text: str
    data: dict[str, Any]


@dataclass
class CommandResolution:
    argv: list[str]

    @property
    def display(self) -> str:
        return " ".join(self.argv)


class ClaudeCodeClient:
    """Thin non-interactive Claude Code CLI adapter.

    Claude Code must be installed and authenticated separately. The default
    command uses `claude -p` so it can be called from an orchestrator script.
    """

    def __init__(
        self,
        command: str = "claude",
        cwd: Path | None = None,
        timeout_seconds: int = 600,
        extra_args: list[str] | None = None,
        heartbeat_seconds: int = 30,
    ):
        self.command = command
        self.cwd = cwd
        self.timeout_seconds = timeout_seconds
        self.extra_args = extra_args or []
        self.heartbeat_seconds = heartbeat_seconds

    def ensure_available(self) -> CommandResolution:
        resolved = resolve_command(self.command)
        if resolved is None:
            raise AgentError(
                f"Claude Code CLI command '{self.command}' was not found. "
                "Install and authenticate Claude Code, set agent.command to an absolute path, "
                "or ensure the nvm bin directory is visible through PATH/NVM_DIR."
            )
        return resolved

    def run_json(self, prompt: str, label: str = "agent") -> AgentResult:
        command = self.ensure_available()
        cmd = [*command.argv, "-p", prompt, "--output-format", "text", *self.extra_args]
        started_at = datetime.now()
        print(
            f"[{started_at.strftime('%Y-%m-%d %H:%M:%S')}] {label} Claude call started "
            f"(timeout={self.timeout_seconds}s)",
            flush=True,
        )
        if command.argv != [self.command]:
            print(f"[{started_at.strftime('%Y-%m-%d %H:%M:%S')}] resolved Claude command: {command.display}", flush=True)
        proc = subprocess.Popen(
            cmd,
            cwd=str(self.cwd) if self.cwd else None,
            stdin=subprocess.DEVNULL,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []

        stdout_thread = threading.Thread(
            target=_collect_stream,
            args=(proc.stdout, stdout_lines, None),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_collect_stream,
            args=(proc.stderr, stderr_lines, f"{label} stderr"),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()

        deadline = time.monotonic() + self.timeout_seconds
        next_heartbeat = time.monotonic() + self.heartbeat_seconds
        while proc.poll() is None:
            now = time.monotonic()
            if now >= deadline:
                proc.kill()
                stdout_thread.join(timeout=1)
                stderr_thread.join(timeout=1)
                raise AgentError(
                    f"Claude Code agent call timed out after {self.timeout_seconds}s.\n"
                    f"Command: {' '.join(cmd[:1])} -p <prompt>\n"
                    f"stdout so far:\n{''.join(stdout_lines).strip()}\n"
                    f"stderr so far:\n{''.join(stderr_lines).strip()}"
                )
            if now >= next_heartbeat:
                elapsed = (datetime.now() - started_at).total_seconds()
                print(
                    f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                    f"{label} Claude call still running ({elapsed:.1f}s, pid={proc.pid})",
                    flush=True,
                )
                next_heartbeat = now + self.heartbeat_seconds
            time.sleep(0.2)

        stdout_thread.join(timeout=1)
        stderr_thread.join(timeout=1)
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()
        elapsed = (datetime.now() - started_at).total_seconds()
        stdout = "".join(stdout_lines)
        stderr = "".join(stderr_lines)
        if proc.returncode != 0:
            raise AgentError(
                "Claude Code agent call failed.\n"
                f"Command: {' '.join(cmd[:1])} -p <prompt>\n"
                f"stdout:\n{stdout.strip()}\n"
                f"stderr:\n{stderr.strip()}"
            )
        print(
            f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
            f"{label} Claude call finished ({elapsed:.1f}s)",
            flush=True,
        )
        data = extract_json_object(stdout)
        return AgentResult(text=stdout, data=data)


def _collect_stream(stream, buffer: list[str], label: str | None) -> None:
    if stream is None:
        return
    for line in stream:
        buffer.append(line)
        if label:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {label}: {line.rstrip()}", flush=True)


def resolve_command(command: str) -> CommandResolution | None:
    found = shutil.which(command)
    if found:
        return CommandResolution([found])
    if Path(command).is_absolute() or "/" in command:
        path = Path(command).expanduser()
        return CommandResolution([str(path)]) if path.exists() else None
    if command != "claude":
        return None

    candidates: list[Path] = []
    nvm_dir = os.environ.get("NVM_DIR")
    nvm_roots: list[Path] = []
    if nvm_dir:
        nvm_roots.append(Path(nvm_dir).expanduser() / "versions" / "node")
    nvm_roots.append(Path.home() / ".nvm" / "versions" / "node")
    for node_versions in nvm_roots:
        candidates.extend(sorted(node_versions.glob("*/bin/claude"), reverse=True))

    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return CommandResolution([str(candidate)])
    for node_versions in nvm_roots:
        for wrapper in sorted(
            node_versions.glob("*/lib/node_modules/@anthropic-ai/claude-code/cli-wrapper.cjs"),
            reverse=True,
        ):
            node = wrapper.parents[4] / "bin" / "node"
            if wrapper.is_file() and node.is_file() and os.access(node, os.X_OK):
                return CommandResolution([str(node), str(wrapper)])
    return None


def extract_json_object(text: str) -> dict[str, Any]:
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    candidates = [fenced.group(1)] if fenced else []
    candidates.append(text.strip())

    # Last resort: find the outermost JSON-looking object.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(text[start : end + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise AgentError(f"Agent did not return a parseable JSON object. Raw output:\n{text}")
