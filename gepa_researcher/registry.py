from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from .io_utils import read_json, write_json
from .schemas import AdmissionDecision, ExecutionRecord, ProvenanceReport, WorkspaceLease


class ExecutionRegistry:
    """Durable source of truth for candidate/workspace/commit attribution."""

    def __init__(self, run_dir: Path):
        self.path = run_dir / "execution_registry.json"
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {
            "admissions": {},
            "workspaces": {},
            "executions": {},
            "provenance": {},
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

    def record_provenance(self, report: ProvenanceReport) -> None:
        self._put("provenance", report.candidate_id, report.to_dict())

    def mark_candidate_status(self, candidate_id: str, status: str) -> None:
        self._put("candidate_status", candidate_id, status)

    def workspace(self, candidate_id: str) -> dict[str, Any] | None:
        value = self._data["workspaces"].get(candidate_id)
        return dict(value) if value else None

    def execution(self, candidate_id: str) -> dict[str, Any] | None:
        value = self._data["executions"].get(candidate_id)
        return dict(value) if value else None

    def verified_result_sha(self, candidate_id: str, require_accepted: bool = True) -> str | None:
        provenance = dict(self._data["provenance"].get(candidate_id) or {})
        execution = dict(self._data["executions"].get(candidate_id) or {})
        if not provenance.get("verified"):
            return None
        if require_accepted and self._data["candidate_status"].get(candidate_id) != "accepted":
            return None
        return provenance.get("result_sha") or execution.get("result_sha")

    def known_candidate_ids(self) -> set[str]:
        ids: set[str] = set()
        for key in ("admissions", "workspaces", "executions", "candidate_status"):
            ids.update(self._data[key])
        return ids

    def _put(self, section: str, key: str, value: Any) -> None:
        with self._lock:
            self._data[section][key] = value
            write_json(self.path, self._data)
