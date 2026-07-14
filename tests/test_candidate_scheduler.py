from __future__ import annotations

import pytest

from gepa_researcher.domain.candidate import CandidateCard, CandidateStatus, ProposalIdea
from gepa_researcher.domain.execution import ExecutionPhase
from gepa_researcher.services.candidate_factory import CandidateFactory, ParentNotMaterialized
from gepa_researcher.services.candidate_scheduler import CandidateScheduler


def _proposal(proposal_id: str = "proposal-1") -> ProposalIdea:
    return ProposalIdea(
        proposal_id=proposal_id,
        hypothesis="h",
        scope="src/hot.cc",
        proposed_change="change",
        rationale="why",
        expected_improvement="score",
        risk="risk",
        prompt_text="prompt",
        target_files=("src/hot.cc",),
    )


def _card(candidate_id: str, *, result_revision: str | None = None) -> CandidateCard:
    proposal = _proposal(candidate_id)
    return CandidateCard(
        candidate_id=candidate_id,
        round_id=0,
        parent_candidate_ids=(),
        proposal_id=proposal.proposal_id,
        proposal=proposal,
        base_revision="0" * 40,
        result_revision=result_revision,
        status=CandidateStatus.MATERIALIZED if result_revision else CandidateStatus.ADMITTED,
    )


def test_child_base_revision_uses_code_base_parent_result_revision():
    parent_a = _card("parent-a", result_revision="a" * 40)
    parent_b = _card("parent-b", result_revision="b" * 40)
    child = CandidateFactory(run_id="run-001").create_child(
        round_id=1,
        parent_cards=[parent_a, parent_b],
        proposal=_proposal("child-proposal"),
        code_base_parent_id="parent-b",
        candidate_id="cand_001_000",
    )

    assert child.candidate_id == "cand_001_000"
    assert child.parent_candidate_ids == ("parent-a", "parent-b")
    assert child.base_revision == "b" * 40
    assert child.status == CandidateStatus.GENERATED


def test_child_requires_materialized_code_base_parent():
    parent = _card("parent-a", result_revision=None)

    with pytest.raises(ParentNotMaterialized):
        CandidateFactory(run_id="run-001").create_child(
            round_id=1,
            parent_cards=[parent],
            proposal=_proposal("child-proposal"),
            code_base_parent_id="parent-a",
        )


def test_scheduler_uses_base_revision_for_implementation_and_result_revision_for_eval():
    card = _card("cand_001_000", result_revision=None)
    card.base_revision = "a" * 40
    scheduler = CandidateScheduler(run_id="run-001", wall_seconds=300)

    impl = scheduler.make_implementation(card)

    assert impl.phase == ExecutionPhase.IMPLEMENTATION
    assert impl.input_revision == "a" * 40
    assert impl.capability_policy.repo_writable is True

    card.result_revision = "b" * 40
    card.status = CandidateStatus.MATERIALIZED
    feedback = scheduler.make_feedback_eval(card, dataset_ref="feedback:round-1")

    assert feedback.phase == ExecutionPhase.FEEDBACK_EVAL
    assert feedback.input_revision == "b" * 40
    assert feedback.capability_policy.repo_writable is False


def test_next_execution_returns_none_for_terminal_cards():
    scheduler = CandidateScheduler(run_id="run-001")
    card = _card("cand_001_000", result_revision="b" * 40)
    card.status = CandidateStatus.ACCEPTED

    assert scheduler.next_execution(card) is None
