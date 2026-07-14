from __future__ import annotations

import uuid

from ..domain.candidate import CandidateCard, CandidateStatus, ProposalIdea


class ParentNotMaterialized(RuntimeError):
    pass


class CandidateFactory:
    def __init__(self, run_id: str):
        self.run_id = run_id

    def create_seed(
        self,
        *,
        round_id: int,
        proposal: ProposalIdea,
        baseline_revision: str,
        candidate_id: str | None = None,
    ) -> CandidateCard:
        return CandidateCard(
            candidate_id=candidate_id or _candidate_id("seed", round_id),
            round_id=round_id,
            parent_candidate_ids=(),
            proposal_id=proposal.proposal_id,
            proposal=proposal,
            base_revision=baseline_revision,
            status=CandidateStatus.GENERATED,
        )

    def create_child(
        self,
        *,
        round_id: int,
        parent_cards: list[CandidateCard],
        proposal: ProposalIdea,
        code_base_parent_id: str,
        candidate_id: str | None = None,
    ) -> CandidateCard:
        parents_by_id = {card.candidate_id: card for card in parent_cards}
        parent = parents_by_id.get(code_base_parent_id)
        if parent is None:
            raise KeyError(f"unknown code_base_parent_id: {code_base_parent_id}")
        if parent.result_revision is None:
            raise ParentNotMaterialized(f"parent has no result_revision: {code_base_parent_id}")
        return CandidateCard(
            candidate_id=candidate_id or _candidate_id("cand", round_id),
            round_id=round_id,
            parent_candidate_ids=tuple(card.candidate_id for card in parent_cards),
            proposal_id=proposal.proposal_id,
            proposal=proposal,
            base_revision=parent.result_revision,
            status=CandidateStatus.GENERATED,
        )


def _candidate_id(prefix: str, round_id: int) -> str:
    return f"{prefix}_{round_id:03d}_{uuid.uuid4().hex[:8]}"
