from __future__ import annotations

import uuid

from ..domain.candidate import CandidateCard, CandidateStatus
from ..domain.execution import CapabilityPolicy, ExecutionBudget, ExecutionPhase, ExecutionSpec


class CandidateScheduler:
    def __init__(
        self,
        *,
        run_id: str,
        wall_seconds: int = 600,
        allowed_tools: tuple[str, ...] = ("bash", "git"),
        forbidden_paths: tuple[str, ...] = (),
        network_allowed: bool = False,
    ):
        self.run_id = run_id
        self.wall_seconds = wall_seconds
        self.allowed_tools = allowed_tools
        self.forbidden_paths = forbidden_paths
        self.network_allowed = network_allowed

    def next_execution(self, card: CandidateCard) -> ExecutionSpec | None:
        if card.status == CandidateStatus.ADMITTED:
            return self.make_implementation(card)
        if card.status == CandidateStatus.MATERIALIZED:
            return self.make_feedback_eval(card, dataset_ref=None)
        return None

    def make_implementation(self, card: CandidateCard) -> ExecutionSpec:
        return self._spec(
            card,
            phase=ExecutionPhase.IMPLEMENTATION,
            input_revision=card.base_revision,
            repo_writable=True,
            dataset_ref=None,
            allowed_target_files=card.proposal.target_files,
        )

    def make_feedback_eval(self, card: CandidateCard, dataset_ref: str | None) -> ExecutionSpec:
        if card.result_revision is None:
            raise ValueError(f"candidate has no result_revision for feedback eval: {card.candidate_id}")
        return self._spec(
            card,
            phase=ExecutionPhase.FEEDBACK_EVAL,
            input_revision=card.result_revision,
            repo_writable=False,
            dataset_ref=dataset_ref,
        )

    def make_pareto_eval(self, card: CandidateCard, dataset_ref: str | None) -> ExecutionSpec:
        if card.result_revision is None:
            raise ValueError(f"candidate has no result_revision for pareto eval: {card.candidate_id}")
        return self._spec(
            card,
            phase=ExecutionPhase.PARETO_EVAL,
            input_revision=card.result_revision,
            repo_writable=False,
            dataset_ref=dataset_ref,
        )

    def _spec(
        self,
        card: CandidateCard,
        *,
        phase: ExecutionPhase,
        input_revision: str,
        repo_writable: bool,
        dataset_ref: str | None,
        allowed_target_files: tuple[str, ...] = (),
    ) -> ExecutionSpec:
        return ExecutionSpec(
            execution_id=_execution_id(card, phase),
            run_id=self.run_id,
            round_id=card.round_id,
            candidate_id=card.candidate_id,
            phase=phase,
            input_revision=input_revision,
            dataset_ref=dataset_ref,
            evaluator_version=None,
            budget=ExecutionBudget(wall_seconds=self.wall_seconds),
            capability_policy=CapabilityPolicy(
                repo_writable=repo_writable,
                network_allowed=self.network_allowed,
                allowed_tools=self.allowed_tools,
                forbidden_paths=self.forbidden_paths,
                allowed_target_files=allowed_target_files,
            ),
        )


def _execution_id(card: CandidateCard, phase: ExecutionPhase) -> str:
    return f"exec_{card.round_id:03d}_{card.candidate_id}_{phase.value}_{uuid.uuid4().hex[:8]}"
