"""Domain objects for the GEPA Candidate Execution Kernel."""

from .artifact import ArtifactKind, ArtifactRef
from .candidate import CandidateCard, CandidateStatus, ProposalIdea
from .execution import (
    CapabilityPolicy,
    ExecutionBudget,
    ExecutionFailure,
    ExecutionPhase,
    ExecutionRecord,
    ExecutionSpec,
    ExecutionStatus,
)
from .revision import RevisionRef

__all__ = [
    "ArtifactKind",
    "ArtifactRef",
    "CandidateCard",
    "CandidateStatus",
    "CapabilityPolicy",
    "ExecutionBudget",
    "ExecutionFailure",
    "ExecutionPhase",
    "ExecutionRecord",
    "ExecutionSpec",
    "ExecutionStatus",
    "ProposalIdea",
    "RevisionRef",
]
