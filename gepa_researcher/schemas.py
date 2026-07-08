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
    target_module: str
    proposed_change: str
    rationale: str
    expected_improvement: str
    risk: str
    prompt_text: str
    created_at: str
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
