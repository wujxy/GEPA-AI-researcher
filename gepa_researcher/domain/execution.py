from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from .artifact import ArtifactRef
from .revision import RevisionRef


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ExecutionPhase(str, Enum):
    IMPLEMENTATION = "implementation"
    FEEDBACK_EVAL = "feedback_eval"
    PARETO_EVAL = "pareto_eval"
    ROBUSTNESS_EVAL = "robustness_eval"
    REPAIR = "repair"


class ExecutionStatus(str, Enum):
    PENDING = "pending"
    PREPARING = "preparing"
    RUNNING = "running"
    COLLECTING = "collecting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class ExecutionBudget:
    wall_seconds: int
    max_tokens: int | None = None
    max_files_changed: int | None = None
    max_commands: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExecutionBudget":
        return cls(
            wall_seconds=int(data["wall_seconds"]),
            max_tokens=data.get("max_tokens"),
            max_files_changed=data.get("max_files_changed"),
            max_commands=data.get("max_commands"),
        )


@dataclass(frozen=True)
class CapabilityPolicy:
    repo_writable: bool
    network_allowed: bool
    allowed_tools: tuple[str, ...] = ()
    forbidden_paths: tuple[str, ...] = ()
    # §4.8: per-candidate target files (from ProposalIdea.target_files) that
    # the harness commit step is allowed to stage. Empty means "fall back to
    # candidate_policy.allowed_target_globs" (global). Admission already
    # validated these against allowed_target_globs / frozen_globs, so by the
    # time an ExecutionSpec reaches execution the targets are pre-cleared.
    allowed_target_files: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_writable": self.repo_writable,
            "network_allowed": self.network_allowed,
            "allowed_tools": list(self.allowed_tools),
            "forbidden_paths": list(self.forbidden_paths),
            "allowed_target_files": list(self.allowed_target_files),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CapabilityPolicy":
        return cls(
            repo_writable=bool(data.get("repo_writable", False)),
            network_allowed=bool(data.get("network_allowed", False)),
            allowed_tools=tuple(map(str, data.get("allowed_tools") or ())),
            forbidden_paths=tuple(map(str, data.get("forbidden_paths") or ())),
            allowed_target_files=tuple(map(str, data.get("allowed_target_files") or ())),
        )


@dataclass(frozen=True)
class ExecutionSpec:
    execution_id: str
    run_id: str
    round_id: int
    candidate_id: str
    phase: ExecutionPhase
    input_revision: str
    dataset_ref: str | None
    evaluator_version: str | None
    budget: ExecutionBudget
    capability_policy: CapabilityPolicy
    created_at: str = field(default_factory=_now_iso)

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_revision", RevisionRef.validate_sha(self.input_revision))
        if isinstance(self.phase, str):
            object.__setattr__(self, "phase", ExecutionPhase(self.phase))

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "run_id": self.run_id,
            "round_id": self.round_id,
            "candidate_id": self.candidate_id,
            "phase": self.phase.value,
            "input_revision": self.input_revision,
            "dataset_ref": self.dataset_ref,
            "evaluator_version": self.evaluator_version,
            "budget": self.budget.to_dict(),
            "capability_policy": self.capability_policy.to_dict(),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExecutionSpec":
        return cls(
            execution_id=str(data["execution_id"]),
            run_id=str(data["run_id"]),
            round_id=int(data["round_id"]),
            candidate_id=str(data["candidate_id"]),
            phase=ExecutionPhase(str(data["phase"])),
            input_revision=str(data["input_revision"]),
            dataset_ref=data.get("dataset_ref"),
            evaluator_version=data.get("evaluator_version"),
            budget=ExecutionBudget.from_dict(dict(data["budget"])),
            capability_policy=CapabilityPolicy.from_dict(dict(data["capability_policy"])),
            created_at=str(data.get("created_at") or _now_iso()),
        )


class ExecutionFailureCode(str, Enum):
    """Typed execution failure codes.

    ``str, Enum`` so existing ``record.failure.code == "COMMIT_FAILED"``
    string comparisons keep working unchanged -- the enum member compares
    equal to its string value. ``to_dict`` stores ``.value`` (str) for
    backward-compatible serialization; ``from_dict`` rehydrates via the enum
    and tolerates unknown strings (old stored records) by falling back to a
    raw str.
    """

    # v1.x existing (from _failure_from_exception + execution_store)
    SANDBOX_PREPARE_FAILED = "SANDBOX_PREPARE_FAILED"
    RUNTIME_PREPARE_FAILED = "RUNTIME_PREPARE_FAILED"
    AGENT_PROTOCOL_INVALID = "AGENT_PROTOCOL_INVALID"
    AGENT_PROCESS_FAILED = "AGENT_PROCESS_FAILED"
    COMMIT_FAILED = "COMMIT_FAILED"
    READONLY_EXECUTION_MUTATED_REPO = "READONLY_EXECUTION_MUTATED_REPO"
    SANDBOX_CLEANUP_FAILED = "SANDBOX_CLEANUP_FAILED"
    RUN_INTERRUPTED = "RUN_INTERRUPTED"
    # §4.8 split (Part A.4): NoCandidateCommit decomposed into three causes
    NO_CANDIDATE_COMMIT = "NO_CANDIDATE_COMMIT"
    NO_CANDIDATE_COMMIT_EMPTY = "NO_CANDIDATE_COMMIT_EMPTY"
    NO_CANDIDATE_COMMIT_ONLY_FORBIDDEN = "NO_CANDIDATE_COMMIT_ONLY_FORBIDDEN"
    NO_CANDIDATE_COMMIT_NONE_ALLOWED = "NO_CANDIDATE_COMMIT_NONE_ALLOWED"
    # v2.0 executor-loop terminal states (docs §6)
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    ENVIRONMENT_BLOCKED = "ENVIRONMENT_BLOCKED"
    PROPOSAL_INFEASIBLE = "PROPOSAL_INFEASIBLE"
    SCOPE_VIOLATION = "SCOPE_VIOLATION"
    NO_PROGRESS = "NO_PROGRESS"
    # v2.0 role failures (alpha: PLAN_INVALID, beta: CRITIC_ABORTED)
    PLAN_INVALID = "PLAN_INVALID"
    CRITIC_ABORTED = "CRITIC_ABORTED"


@dataclass
class ExecutionFailure:
    code: ExecutionFailureCode | str
    message: str
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        code = self.code
        if isinstance(code, ExecutionFailureCode):
            code = code.value
        data = asdict(self)
        data["code"] = code
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ExecutionFailure | None":
        if not data:
            return None
        raw_code = str(data["code"])
        try:
            code: ExecutionFailureCode | str = ExecutionFailureCode(raw_code)
        except ValueError:
            # Tolerate unknown / legacy code strings without crashing.
            code = raw_code
        return cls(
            code=code,
            message=str(data.get("message", "")),
            retryable=bool(data.get("retryable", False)),
            details=dict(data.get("details") or {}),
        )


@dataclass
class ExecutionRecord:
    execution_id: str
    candidate_id: str
    phase: ExecutionPhase
    input_revision: str
    status: ExecutionStatus
    round_id: int | None = None
    run_id: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    result_revision: str | None = None
    metrics: dict[str, float] = field(default_factory=dict)
    artifact_refs: list[ArtifactRef] = field(default_factory=list)
    failure: ExecutionFailure | None = None
    environment_hash: str | None = None
    dataset_hash: str | None = None
    evaluator_version: str | None = None
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)

    def __post_init__(self) -> None:
        if isinstance(self.phase, str):
            self.phase = ExecutionPhase(self.phase)
        if isinstance(self.status, str):
            self.status = ExecutionStatus(self.status)
        self.input_revision = RevisionRef.validate_sha(self.input_revision)
        if self.result_revision is not None:
            self.result_revision = RevisionRef.validate_sha(self.result_revision)

    @classmethod
    def from_spec(cls, spec: ExecutionSpec) -> "ExecutionRecord":
        return cls(
            execution_id=spec.execution_id,
            candidate_id=spec.candidate_id,
            round_id=spec.round_id,
            run_id=spec.run_id,
            phase=spec.phase,
            input_revision=spec.input_revision,
            status=ExecutionStatus.PENDING,
            evaluator_version=spec.evaluator_version,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "candidate_id": self.candidate_id,
            "phase": self.phase.value,
            "input_revision": self.input_revision,
            "status": self.status.value,
            "round_id": self.round_id,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "result_revision": self.result_revision,
            "metrics": dict(self.metrics),
            "artifact_refs": [artifact.to_dict() for artifact in self.artifact_refs],
            "failure": self.failure.to_dict() if self.failure else None,
            "environment_hash": self.environment_hash,
            "dataset_hash": self.dataset_hash,
            "evaluator_version": self.evaluator_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExecutionRecord":
        return cls(
            execution_id=str(data["execution_id"]),
            candidate_id=str(data["candidate_id"]),
            phase=ExecutionPhase(str(data["phase"])),
            input_revision=str(data["input_revision"]),
            status=ExecutionStatus(str(data["status"])),
            round_id=data.get("round_id"),
            run_id=data.get("run_id"),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            result_revision=data.get("result_revision"),
            metrics={str(key): float(value) for key, value in dict(data.get("metrics") or {}).items()},
            artifact_refs=[ArtifactRef.from_dict(dict(item)) for item in data.get("artifact_refs") or []],
            failure=ExecutionFailure.from_dict(data.get("failure")),
            environment_hash=data.get("environment_hash"),
            dataset_hash=data.get("dataset_hash"),
            evaluator_version=data.get("evaluator_version"),
            created_at=str(data.get("created_at") or _now_iso()),
            updated_at=str(data.get("updated_at") or _now_iso()),
        )
