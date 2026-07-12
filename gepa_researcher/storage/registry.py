from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from .io_utils import read_json, write_json
from ..models.schemas import AdmissionDecision, ExecutionRecord, WorkspaceLease


class ExecutionRegistry:
    """Durable source of truth for candidate/workspace/commit attribution."""

    def __init__(self, run_dir: Path):
        self.path = run_dir / "execution_registry.json"
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {
            "admissions": {},
            "workspaces": {},
            "executions": {},
            "candidate_status": {},
        }
        if self.path.exists():
            loaded = read_json(self.path)
            for key in self._data:
                self._data[key] = dict(loaded.get(key, {}))

    def record_admission(self, decision: AdmissionDecision) -> None:
        self._put("admissions", decision.candidate_id, decision.to_dict())

    def record_workspace(self, lease: WorkspaceLease) -> None:
        self._put("workspaces", lease.candidate_id, lease.to_dict())

    def record_execution(self, record: ExecutionRecord) -> None:
        self._put("executions", record.candidate_id, record.to_dict())

    def mark_candidate_status(self, candidate_id: str, status: str) -> None:
        self._put("candidate_status", candidate_id, status)

    def workspace(self, candidate_id: str) -> dict[str, Any] | None:
        value = self._data["workspaces"].get(candidate_id)
        return dict(value) if value else None

    def execution(self, candidate_id: str) -> dict[str, Any] | None:
        value = self._data["executions"].get(candidate_id)
        return dict(value) if value else None

    def accepted_result_sha(self, candidate_id: str, require_accepted: bool = True) -> str | None:
        """Result commit SHA for a candidate, gated on acceptance.

        Replaces the former ``verified_result_sha``: there is no separate
        "provenance verification" gate anymore, so stackability is simply
        "the candidate was accepted and recorded a commit".
        """
        execution = dict(self._data["executions"].get(candidate_id) or {})
        if require_accepted and self._data["candidate_status"].get(candidate_id) != "accepted":
            return None
        return execution.get("result_sha")

    def known_candidate_ids(self) -> set[str]:
        ids: set[str] = set()
        for key in ("admissions", "workspaces", "executions", "candidate_status"):
            ids.update(self._data[key])
        return ids

    def _put(self, section: str, key: str, value: Any) -> None:
        with self._lock:
            self._data[section][key] = value
            write_json(self.path, self._data)
