from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from ..models.schemas import Candidate
from .revision import RevisionRef


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class CandidateStatus(str, Enum):
    GENERATED = "generated"
    ADMITTED = "admitted"
    MATERIALIZING = "materializing"
    MATERIALIZED = "materialized"
    EVALUATING = "evaluating"
    EVALUATED = "evaluated"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    IMPLEMENTATION_FAILED = "implementation_failed"
    EVALUATION_FAILED = "evaluation_failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class ProposalIdea:
    proposal_id: str
    hypothesis: str
    scope: str
    proposed_change: str
    rationale: str
    expected_improvement: str
    risk: str
    prompt_text: str
    target_files: tuple[str, ...] = ()
    executor_contract: dict[str, Any] = field(default_factory=dict)
    expected_artifacts: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_candidate(cls, candidate: Candidate) -> "ProposalIdea":
        return cls(
            proposal_id=candidate.candidate_id,
            hypothesis=candidate.hypothesis,
            scope=candidate.scope,
            proposed_change=candidate.proposed_change,
            rationale=candidate.rationale,
            expected_improvement=candidate.expected_improvement,
            risk=candidate.risk,
            prompt_text=candidate.prompt_text,
            target_files=tuple(candidate.target_files),
            executor_contract=dict(candidate.executor_contract),
            expected_artifacts=tuple(candidate.expected_artifacts),
            metadata=dict(candidate.artifacts),
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["target_files"] = list(self.target_files)
        data["expected_artifacts"] = list(self.expected_artifacts)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProposalIdea":
        return cls(
            proposal_id=str(data["proposal_id"]),
            hypothesis=str(data.get("hypothesis", "")),
            scope=str(data.get("scope", "")),
            proposed_change=str(data.get("proposed_change", "")),
            rationale=str(data.get("rationale", "")),
            expected_improvement=str(data.get("expected_improvement", "")),
            risk=str(data.get("risk", "")),
            prompt_text=str(data.get("prompt_text", "")),
            target_files=tuple(map(str, data.get("target_files") or ())),
            executor_contract=dict(data.get("executor_contract") or {}),
            expected_artifacts=tuple(map(str, data.get("expected_artifacts") or ())),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass
class CandidateCard:
    candidate_id: str
    round_id: int
    parent_candidate_ids: tuple[str, ...]
    proposal_id: str
    proposal: ProposalIdea
    base_revision: str
    status: CandidateStatus = CandidateStatus.GENERATED
    result_revision: str | None = None
    execution_ids: list[str] = field(default_factory=list)
    judgment_ids: list[str] = field(default_factory=list)
    artifact_ids: list[str] = field(default_factory=list)
    final_decision: str | None = None
    score_summary: dict[str, float] = field(default_factory=dict)
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)

    def __post_init__(self) -> None:
        self.base_revision = RevisionRef.validate_sha(self.base_revision)
        if self.result_revision is not None:
            self.result_revision = RevisionRef.validate_sha(self.result_revision)
        self.parent_candidate_ids = tuple(map(str, self.parent_candidate_ids))
        if isinstance(self.status, str):
            self.status = CandidateStatus(self.status)

    def touch(self) -> None:
        self.updated_at = _now_iso()

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "round_id": self.round_id,
            "parent_candidate_ids": list(self.parent_candidate_ids),
            "proposal_id": self.proposal_id,
            "proposal": self.proposal.to_dict(),
            "base_revision": self.base_revision,
            "status": self.status.value,
            "result_revision": self.result_revision,
            "execution_ids": list(self.execution_ids),
            "judgment_ids": list(self.judgment_ids),
            "artifact_ids": list(self.artifact_ids),
            "final_decision": self.final_decision,
            "score_summary": dict(self.score_summary),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CandidateCard":
        return cls(
            candidate_id=str(data["candidate_id"]),
            round_id=int(data["round_id"]),
            parent_candidate_ids=tuple(map(str, data.get("parent_candidate_ids") or ())),
            proposal_id=str(data["proposal_id"]),
            proposal=ProposalIdea.from_dict(dict(data["proposal"])),
            base_revision=str(data["base_revision"]),
            status=CandidateStatus(str(data.get("status") or CandidateStatus.GENERATED.value)),
            result_revision=data.get("result_revision"),
            execution_ids=list(map(str, data.get("execution_ids") or [])),
            judgment_ids=list(map(str, data.get("judgment_ids") or [])),
            artifact_ids=list(map(str, data.get("artifact_ids") or [])),
            final_decision=data.get("final_decision"),
            score_summary={str(key): float(value) for key, value in dict(data.get("score_summary") or {}).items()},
            created_at=str(data.get("created_at") or _now_iso()),
            updated_at=str(data.get("updated_at") or _now_iso()),
        )
