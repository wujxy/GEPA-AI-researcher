from __future__ import annotations

import json
import os
import re
import signal
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..models.schemas import AgentCallContext, AgentCallRecord
from ..storage.usage import UsageTracker, normalize_usage


class AgentError(RuntimeError):
    pass


@dataclass
class AgentResult:
    text: str
    data: dict[str, Any]
    envelope: dict[str, Any] | None = None
    call_record: AgentCallRecord | None = None


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
        usage_tracker: UsageTracker | None = None,
        model: str | None = None,
        env: dict[str, str] | None = None,
    ):
        self.command = command
        self.cwd = cwd
        self.timeout_seconds = timeout_seconds
        self.extra_args = extra_args or []
        self.heartbeat_seconds = heartbeat_seconds
        self.usage_tracker = usage_tracker
        self.model = model
        self.env = env or {}

    def ensure_available(self) -> CommandResolution:
        resolved = resolve_command(self.command)
        if resolved is None:
            raise AgentError(
                f"Claude Code CLI command '{self.command}' was not found. "
                "Install and authenticate Claude Code, set agent.command to an absolute path, "
                "or ensure the nvm bin directory is visible through PATH/NVM_DIR."
            )
        return resolved

    def run_json(
        self,
        prompt: str,
        label: str = "agent",
        *,
        call_context: AgentCallContext | None = None,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        extra_args: list[str] | None = None,
        command_prefix: list[str] | None = None,
        inherit_host_env: bool = True,
        resolve_command_on_host: bool = True,
    ) -> AgentResult:
        command = self.ensure_available() if resolve_command_on_host else CommandResolution([self.command])
        args = _without_output_format([*self.extra_args, *(extra_args or [])])
        model_flag = ["--model", self.model] if self.model else []
        settings_flag: list[str] = []
        if self.env:
            settings_flag = ["--settings", json.dumps({"env": self.env})]
        cmd = [*(command_prefix or []), *command.argv, *settings_flag, "-p", prompt, *model_flag, *args, "--output-format", "json"]
        started_at = datetime.now(timezone.utc)
        call_id = str(uuid.uuid4())
        context = call_context or AgentCallContext(role=label, round_id=-1, phase="unspecified")
        print(
            f"[{started_at.astimezone().strftime('%Y-%m-%d %H:%M:%S')}] {label} Claude call started "
            f"(timeout={self.timeout_seconds}s)",
            flush=True,
        )
        if command.argv != [self.command]:
            print(f"[{started_at.astimezone().strftime('%Y-%m-%d %H:%M:%S')}] resolved Claude command: {command.display}", flush=True)
        if command_prefix:
            print(f"[{started_at.astimezone().strftime('%Y-%m-%d %H:%M:%S')}] command prefix: {' '.join(command_prefix)}", flush=True)
        process_env = _process_env({**self.env, **(env or {})}, inherit_host_env=inherit_host_env)
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd or self.cwd) if (cwd or self.cwd) else None,
            env=process_env,
            stdin=subprocess.DEVNULL,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
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
                _terminate_process_group(proc)
                stdout_thread.join(timeout=1)
                stderr_thread.join(timeout=1)
                error = (
                    f"Claude Code agent call timed out after {self.timeout_seconds}s.\n"
                    f"Command: {' '.join(cmd[:1])} -p <prompt>\n"
                    f"stdout so far:\n{''.join(stdout_lines).strip()}\n"
                    f"stderr so far:\n{''.join(stderr_lines).strip()}"
                )
                self._record_call(call_id, context, started_at, "timeout", None, error)
                raise AgentError(error)
            if now >= next_heartbeat:
                elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
                print(
                    f"[{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S')}] "
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
        elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
        stdout = "".join(stdout_lines)
        stderr = "".join(stderr_lines)
        if proc.returncode != 0:
            error = (
                "Claude Code agent call failed.\n"
                f"Command: {' '.join(cmd[:1])} -p <prompt>\n"
                f"stdout:\n{stdout.strip()}\n"
                f"stderr:\n{stderr.strip()}"
            )
            envelope = _try_json_dict(stdout)
            self._record_call(call_id, context, started_at, "failed", envelope, error)
            raise AgentError(error)
        print(
            f"[{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S')}] "
            f"{label} Claude call finished ({elapsed:.1f}s)",
            flush=True,
        )
        result_text = stdout
        try:
            outer = extract_json_object(stdout)
            if isinstance(outer.get("result"), str):
                envelope = outer
                result_text = str(outer["result"])
                data = extract_json_object(result_text)
            else:
                # Compatibility for fake CLIs and older wrappers that print the
                # business JSON directly without a Claude result envelope.
                envelope = {}
                data = outer
        except AgentError as exc:
            envelope = _try_json_dict(stdout) or {}
            self._record_call(call_id, context, started_at, "invalid_result", envelope, str(exc))
            # Surface the agent's raw text so a caller (e.g. the executor repair
            # path) can quote it back in a follow-up "transcribe to JSON" call.
            # result_text is the inner agent string when an envelope parsed, else
            # the full process stdout (initialized before the try).
            exc.raw_output = result_text
            raise
        record = self._record_call(call_id, context, started_at, "completed", envelope, None)
        return AgentResult(text=result_text, data=data, envelope=envelope, call_record=record)

    def _record_call(
        self,
        call_id: str,
        context: AgentCallContext,
        started_at: datetime,
        status: str,
        envelope: dict[str, Any] | None,
        error: str | None,
    ) -> AgentCallRecord:
        finished_at = datetime.now(timezone.utc)
        model_usage = dict((envelope or {}).get("modelUsage") or (envelope or {}).get("model_usage") or {})
        model = (envelope or {}).get("model")
        if model is None and len(model_usage) == 1:
            model = next(iter(model_usage))
        cost = (envelope or {}).get("total_cost_usd")
        try:
            total_cost = float(cost) if cost is not None else None
        except (TypeError, ValueError):
            total_cost = None
        record = AgentCallRecord(
            call_id=call_id,
            context=context,
            status=status,
            started_at=started_at.isoformat(),
            finished_at=finished_at.isoformat(),
            duration_ms=max(0, int((finished_at - started_at).total_seconds() * 1000)),
            usage=normalize_usage(envelope),
            model=str(model) if model is not None else None,
            total_cost_usd=total_cost,
            model_usage=model_usage,
            session_id=(envelope or {}).get("session_id"),
            error=error,
        )
        if self.usage_tracker is not None:
            self.usage_tracker.record(record, envelope)
        return record


def _collect_stream(stream, buffer: list[str], label: str | None) -> None:
    if stream is None:
        return
    for line in stream:
        buffer.append(line)
        if label:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {label}: {line.rstrip()}", flush=True)


def _terminate_process_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=2)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _without_output_format(args: list[str]) -> list[str]:
    result: list[str] = []
    skip_next = False
    for item in args:
        if skip_next:
            skip_next = False
            continue
        if item == "--output-format":
            skip_next = True
            continue
        if item.startswith("--output-format="):
            continue
        result.append(item)
    return result


_CLEAN_ENV_PASSTHROUGH = (
    "LD_LIBRARY_PATH",
    "DYLD_LIBRARY_PATH",
)


def _process_env(env: dict[str, str], *, inherit_host_env: bool) -> dict[str, str]:
    if inherit_host_env:
        return {**os.environ, **env}
    process_env = {
        key: value
        for key in _CLEAN_ENV_PASSTHROUGH
        if (value := os.environ.get(key)) is not None
    }
    process_env.update(env)
    return process_env


def _try_json_dict(text: str) -> dict[str, Any] | None:
    try:
        return extract_json_object(text)
    except AgentError:
        return None


def resolve_command(command: str) -> CommandResolution | None:
    found = shutil.which(command)
    if found:
        return CommandResolution([found])
    if Path(command).is_absolute() or "/" in command:
        path = Path(command).expanduser()
        return CommandResolution([str(path)]) if path.exists() else None
    if command != "claude":
        return None

    nvm_dir = os.environ.get("NVM_DIR")
    nvm_roots: list[Path] = []
    if nvm_dir:
        nvm_roots.append(Path(nvm_dir).expanduser() / "versions" / "node")
    nvm_roots.append(Path.home() / ".nvm" / "versions" / "node")
    for node_versions in nvm_roots:
        for candidate in sorted(node_versions.glob("*/bin/claude"), reverse=True):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return CommandResolution([str(candidate)])
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
