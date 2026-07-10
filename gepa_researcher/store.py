"""RunStore - unified storage abstraction for GEPA research loop.

This class centralizes all state persistence and loading operations,
providing a clean interface for the orchestrator and making testing easier.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .io_utils import append_jsonl, read_json, write_json
from .schemas import (
    Candidate,
    CandidateBatch,
    EvaluationBatch,
    GateDecision,
    GenerationDecision,
    Judgment,
    JudgmentBatch,
    LoopState,
    ParetoFrontier,
    ScoreMatrix,
    Trace,
)


class RunStore:
    """Unified storage manager for GEPA research loop state and artifacts.

    This class handles:
    - State persistence and loading
    - Candidate pool management
    - Score matrix storage
    - Pareto frontier storage
    - Traces and judgments
    - Execution records
    - Generation and gate decisions
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

    # ============================================================================
    # State Management
    # ============================================================================

    def load_or_create_state(self, task_name: str, resume: bool = False) -> LoopState:
        """Load existing state or create new one."""
        state_path = self.run_dir / "state.json"
        if state_path.exists() and resume:
            return LoopState.from_dict(read_json(state_path))
        return LoopState(task_name=task_name)

    def save_state(self, state: LoopState) -> None:
        """Save current loop state."""
        write_json(self.run_dir / "state.json", state.to_dict())

    # ============================================================================
    # Configuration and Runtime Data
    # ============================================================================

    def save_config(self, config: dict[str, Any]) -> None:
        """Save configuration snapshot."""
        self._ensure_directories()  # ✅ 延迟创建目录
        write_json(self.run_dir / "config.snapshot.json", config)

    def save_dataset_split(self, dataset_split: dict[str, Any]) -> None:
        """Save dataset split information."""
        write_json(self.run_dir / "dataset_split.json", dataset_split)

    def save_prior_context(self, prior_context: dict[str, Any]) -> None:
        """Save prior context."""
        write_json(self.run_dir / "prior_context.json", prior_context)

    # ============================================================================
    # Round-level Artifacts
    # ============================================================================

    def _round_dir(self, round_id: int) -> Path:
        """Get directory for round-specific artifacts."""
        return self.run_dir / "traces" / f"round_{round_id:03d}"

    def save_candidate_batch(self, batch: CandidateBatch) -> None:
        """Save candidate batch and individual candidates."""
        round_dir = self._round_dir(batch.round_id)
        write_json(round_dir / "candidate_batch.json", batch.to_dict())
        for candidate in batch.candidates:
            write_json(round_dir / candidate.candidate_id / "candidate.json", candidate.to_dict())
            append_jsonl(self.run_dir / "candidates.jsonl", candidate.to_dict())

    def save_admission_decisions(self, round_id: int, decisions: list[Any]) -> None:
        """Save admission gate decisions."""
        round_dir = self._round_dir(round_id)
        payload = {"round_id": round_id, "decisions": [decision.to_dict() for decision in decisions]}
        write_json(round_dir / "admission_decisions.json", payload)
        for decision in decisions:
            append_jsonl(self.run_dir / "admission_decisions.jsonl", decision.to_dict())

    def save_trace(self, trace: Trace) -> None:
        """Save execution trace."""
        round_dir = self._round_dir(trace.round_id)
        trace_path = round_dir / trace.candidate_id / "trace.json"
        write_json(trace_path, trace.to_dict())
        append_jsonl(self.run_dir / "traces.jsonl", trace.to_dict())

    def save_judgment(self, judgment: Judgment) -> None:
        """Save judgment result."""
        round_dir = self._round_dir(judgment.round_id)
        write_json(round_dir / judgment.candidate_id / "judgment.json", judgment.to_dict())
        append_jsonl(self.run_dir / "judgments.jsonl", judgment.to_dict())

    def save_judgment_batch(self, batch: JudgmentBatch) -> None:
        """Save judgment batch."""
        round_dir = self._round_dir(batch.round_id)
        write_json(round_dir / "judgment_batch.json", batch.to_dict())

    def save_evaluation_batch(self, batch: EvaluationBatch) -> None:
        """Save evaluation batch metadata."""
        round_dir = self._round_dir(batch.round_id)
        write_json(round_dir / f"evaluation_{batch.phase}.json", batch.to_dict())

    def save_gate_decision(self, decision: GateDecision) -> None:
        """Save gate decision."""
        round_dir = self._round_dir(decision.round_id)
        write_json(round_dir / "gate_decision.json", decision.to_dict())
        append_jsonl(self.run_dir / "gate_decisions.jsonl", decision.to_dict())

    def save_generation_decision(self, decision: GenerationDecision) -> None:
        """Save generation decision."""
        round_dir = self._round_dir(decision.round_id)
        write_json(round_dir / "generation_decision.json", decision.to_dict())
        append_jsonl(self.run_dir / "decisions.jsonl", decision.to_dict())

    def save_pareto_frontier(self, frontier: ParetoFrontier) -> None:
        """Save Pareto frontier (global, not round-specific)."""
        write_json(self.run_dir / "frontier.json", frontier.to_dict())

    def save_score_matrix(self, matrix: ScoreMatrix) -> None:
        """Save score matrix (global, not round-specific)."""
        from .score_matrix import ScoreMatrixBuilder
        ScoreMatrixBuilder.persist(matrix, self.run_dir / "score_matrix.json")

    def load_score_matrix(self, round_id: int = 0) -> ScoreMatrix:
        """Load score matrix."""
        from .score_matrix import ScoreMatrixBuilder
        return ScoreMatrixBuilder.load(self.run_dir / "score_matrix.json", round_id=round_id)

    # ============================================================================
    # Live Artifacts (for real-time monitoring)
    # ============================================================================

    def save_live_artifact(self, round_id: int, name: str, data: dict[str, Any]) -> None:
        """Save live artifact for real-time monitoring."""
        write_json(self.run_dir / "live" / f"round_{round_id:03d}_{name}.json", data)

    # ============================================================================
    # Initialization Artifacts
    # ============================================================================

    def save_initialization(
        self,
        candidate_batch: CandidateBatch,
        trace_batch: Any,
        judgment_batch: JudgmentBatch,
        score_matrix: ScoreMatrix,
    ) -> None:
        """Save initialization artifacts."""
        write_json(
            self.run_dir / "initialization.json",
            {
                "candidate_batch": candidate_batch.to_dict(),
                "trace_batch": trace_batch.to_dict(),
                "judgment_batch": judgment_batch.to_dict(),
                "score_matrix": score_matrix.to_dict(),
            },
        )

    # ============================================================================
    # Final Report
    # ============================================================================

    def save_final_report(self, content: str) -> None:
        """Save final report."""
        (self.run_dir / "final_report.md").write_text(content, encoding="utf-8")