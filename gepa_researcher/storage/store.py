"""RunStore - storage abstraction for the GEPA research loop.

Centralizes the state/config/batch persistence that the orchestrator routes
through it. Per-artifact audit writes (traces, judgments, gate/generation
decisions, frontier, score matrix, live/initialization, final report) live in
the orchestrator and adapters, which call io_utils directly; RunStore does not
duplicate them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import sanitize_snapshot
from .io_utils import append_jsonl, read_json, write_json
from ..models.schemas import CandidateBatch, JudgmentBatch, LoopState


class RunStore:
    """Persistence for GEPA loop state, config, and round-level batches.

    Handles loop state, config snapshot, dataset split, prior context,
    candidate batches, admission decisions, and judgment batches. Other
    artifacts are written directly by the orchestrator/adapters.
    """

    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self._directories_created = False

    def _ensure_directories(self) -> None:
        """Create necessary directory structure (lazy, on first save)."""
        if not self._directories_created:
            (self.run_dir / "traces").mkdir(parents=True, exist_ok=True)
            (self.run_dir / "live").mkdir(parents=True, exist_ok=True)
            self._directories_created = True

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def load_or_create_state(self, task_name: str, resume: bool = False) -> LoopState:
        """Load existing state or create new one."""
        state_path = self.run_dir / "state.json"
        if state_path.exists() and resume:
            return LoopState.from_dict(read_json(state_path))
        return LoopState(task_name=task_name)

    def save_state(self, state: LoopState) -> None:
        """Save current loop state."""
        write_json(self.run_dir / "state.json", state.to_dict())

    # ------------------------------------------------------------------
    # Configuration and runtime data
    # ------------------------------------------------------------------

    def save_config(self, config: dict[str, Any]) -> None:
        """Save configuration snapshot."""
        self._ensure_directories()
        write_json(self.run_dir / "config.snapshot.json", sanitize_snapshot(config))

    def save_dataset_split(self, dataset_split: dict[str, Any]) -> None:
        """Save dataset split information."""
        write_json(self.run_dir / "dataset_split.json", dataset_split)

    def save_prior_context(self, prior_context: dict[str, Any]) -> None:
        """Save prior context."""
        write_json(self.run_dir / "prior_context.json", prior_context)

    # ------------------------------------------------------------------
    # Round-level batches
    # ------------------------------------------------------------------

    def _round_dir(self, round_id: int) -> Path:
        """Get directory for round-specific artifacts."""
        return self.run_dir / "traces" / f"round_{round_id:03d}"

    def save_candidate_batch(self, batch: CandidateBatch) -> None:
        """Save candidate batch and individual candidates."""
        round_dir = self._round_dir(batch.round_id)
        write_json(round_dir / "candidate_batch.json", batch.to_dict())
        for candidate in batch.candidates:
            write_json(round_dir / candidate.candidate_id / "candidate.json", candidate.to_dict())

    def save_admission_decisions(self, round_id: int, decisions: list[Any]) -> None:
        """Save admission gate decisions."""
        round_dir = self._round_dir(round_id)
        payload = {"round_id": round_id, "decisions": [decision.to_dict() for decision in decisions]}
        write_json(round_dir / "admission_decisions.json", payload)
        for decision in decisions:
            append_jsonl(self.run_dir / "admission_decisions.jsonl", decision.to_dict())

    def save_judgment_batch(self, batch: JudgmentBatch) -> None:
        """Save judgment batch."""
        round_dir = self._round_dir(batch.round_id)
        write_json(round_dir / "judgment_batch.json", batch.to_dict())
