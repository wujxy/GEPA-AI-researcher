from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


DecisionKind = Literal["keep", "reject", "iterate", "stop"]


@dataclass
class Candidate:
    candidate_id: str
    round_id: int
    parent_id: str | None
    hypothesis: str
    scope: str
    proposed_change: str
    rationale: str
    expected_improvement: str
    risk: str
    prompt_text: str
    created_at: str
    parent_ids: list[str] = field(default_factory=list)
    generation: int = 0
    executor_contract: dict[str, Any] = field(default_factory=dict)
    expected_artifacts: list[str] = field(default_factory=list)
    mutation_note: str = ""
    merge_note: str = ""
    status: str = "generated"
    artifacts: dict[str, Any] = field(default_factory=dict)
    target_files: list[str] = field(default_factory=list)
    safety_class: str = ""
    strategy: str = ""
    expected_gain: float | None = None
    admission_status: str = "pending"
    admission_decision_id: str | None = None

    def __post_init__(self) -> None:
        if not self.parent_ids and self.parent_id:
            self.parent_ids = [self.parent_id]
        if self.parent_ids and not self.parent_id:
            self.parent_id = self.parent_ids[0]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AdmissionDecision:
    decision_id: str
    candidate_id: str
    round_id: int
    admitted: bool
    checks: dict[str, str]
    failure_codes: list[str] = field(default_factory=list)
    details: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WorkspaceLease:
    candidate_id: str
    round_id: int
    requested_parent_sha: str
    actual_start_sha: str
    branch_name: str
    worktree_path: str
    artifact_path: str
    mode: str = "artifact_directory"
    status: str = "ready"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExecutionRecord:
    execution_id: str
    candidate_id: str
    round_id: int
    parent_candidate_id: str | None
    requested_parent_sha: str
    actual_start_sha: str
    result_sha: str | None
    branch_name: str
    worktree_path: str
    changed_files: list[str] = field(default_factory=list)
    commit_count: int = 0
    execution_mode: str = "implement_and_validate"
    status: str = "created"
    artifacts: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ProvenanceReport:
    execution_id: str
    candidate_id: str
    verified: bool
    checks: dict[str, str]
    failure_codes: list[str] = field(default_factory=list)
    details: list[str] = field(default_factory=list)
    result_sha: str | None = None
    changed_files: list[str] = field(default_factory=list)
    commit_count: int = 0
    artifact_hashes: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AgentCallContext:
    role: str
    round_id: int
    phase: str
    candidate_id: str | None = None
    execution_id: str | None = None
    parent_candidate_id: str | None = None
    candidate_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TokenUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    processed_tokens: int | None = None
    available: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AgentCallRecord:
    call_id: str
    context: AgentCallContext
    status: str
    started_at: str
    finished_at: str
    duration_ms: int
    usage: TokenUsage
    model: str | None = None
    total_cost_usd: float | None = None
    model_usage: dict[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RoundUsageSummary:
    round_id: int
    calls: int
    unavailable_calls: int
    by_role: dict[str, dict[str, Any]]
    by_candidate: dict[str, dict[str, Any]]
    totals: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RunUsageSummary:
    calls: int
    unavailable_calls: int
    by_role: dict[str, dict[str, Any]]
    by_round: dict[str, dict[str, Any]]
    by_candidate: dict[str, dict[str, Any]]
    totals: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CandidateBatch:
    round_id: int
    candidates: list[Candidate]
    artifacts: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SampleTrace:
    sample_id: str
    input: str
    output: str
    expected: str
    logs: str
    error: str | None = None
    latency_ms: int = 0
    artifacts: dict[str, Any] = field(default_factory=dict)


@dataclass
class Trace:
    candidate_id: str
    round_id: int
    samples: list[SampleTrace]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TraceBatch:
    round_id: int
    traces: list[Trace]
    failed_candidate_ids: list[str]
    artifacts: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DatasetSplit:
    feedback_ids: list[str]
    pareto_ids: list[str]
    artifacts: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EvaluationBatch:
    round_id: int
    phase: Literal["feedback", "pareto"]
    candidate_ids: list[str]
    sample_ids: list[str]
    trace_paths: dict[str, str] = field(default_factory=dict)
    judgment_paths: dict[str, str] = field(default_factory=dict)
    artifacts: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Judgment:
    candidate_id: str
    round_id: int
    score: float
    passed: bool
    per_sample_scores: list[dict[str, Any]]
    failure_categories: list[str]
    actionable_feedback: list[str]
    confidence: str
    artifacts: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class JudgmentBatch:
    round_id: int
    judgments: list[Judgment]
    summary: dict[str, Any]
    artifacts: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EvaluationResult:
    candidate_id: str
    task_id: str
    score: float
    passed: bool
    feedback_text: str = ""
    artifacts: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ScoreMatrix:
    round_id: int
    task_scores: dict[str, dict[str, float]] = field(default_factory=dict)
    aggregate_scores: dict[str, float] = field(default_factory=dict)
    artifacts: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScoreMatrix":
        return cls(
            round_id=int(data.get("round_id", 0)),
            task_scores={
                str(task_id): {str(candidate_id): float(score) for candidate_id, score in scores.items()}
                for task_id, scores in dict(data.get("task_scores", {})).items()
            },
            aggregate_scores={
                str(candidate_id): float(score)
                for candidate_id, score in dict(data.get("aggregate_scores", {})).items()
            },
            artifacts=dict(data.get("artifacts", {})),
        )


@dataclass
class ParetoFrontier:
    round_id: int
    candidate_ids: list[str]
    per_task_best: dict[str, list[str]]
    parent_ids: list[str] = field(default_factory=list)
    artifacts: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GateDecision:
    round_id: int
    accepted: list[str]
    discarded: list[str]
    reason_by_candidate: dict[str, str]
    stop: bool = False
    artifacts: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CandidatePoolSnapshot:
    active_candidate_ids: list[str]
    accepted_candidate_ids: list[str]
    discarded_candidate_ids: list[str]
    ancestry: dict[str, list[str]]
    candidates: dict[str, dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Decision:
    candidate_id: str
    round_id: int
    decision: DecisionKind
    reason: str
    best_so_far: str | None
    stop: bool = False
    artifacts: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GenerationDecision:
    round_id: int
    kept: list[str]
    rejected: list[str]
    next_feedback: list[str]
    stop: bool
    artifacts: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LoopState:
    task_name: str
    round_id: int = 0
    best_candidate_id: str | None = None
    best_score: float = -1.0
    no_improvement_rounds: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LoopState":
        return cls(**data)
