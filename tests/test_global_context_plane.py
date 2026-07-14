from gepa_researcher.context.blocks import ContextBlockKind
from gepa_researcher.context.plane import GlobalContextPlane
from gepa_researcher.domain.candidate import CandidateCard, CandidateStatus, ProposalIdea
from gepa_researcher.models.schemas import LoopState
from gepa_researcher.storage.artifact_store import ArtifactStore
from gepa_researcher.storage.candidate_store import CandidateStore
from gepa_researcher.storage.event_store import EventStore
from gepa_researcher.storage.execution_store import ExecutionStore
from gepa_researcher.storage.store import RunStore


def _candidate_card() -> CandidateCard:
    return CandidateCard(
        candidate_id="cand_001",
        round_id=1,
        parent_candidate_ids=(),
        proposal_id="cand_001",
        proposal=ProposalIdea(
            proposal_id="cand_001",
            hypothesis="try smaller feature set",
            scope="model inputs",
            proposed_change="reduce inputs",
            rationale="remove noisy features",
            expected_improvement="improve metric",
            risk="may lose useful signal",
            prompt_text="reduce inputs",
        ),
        base_revision="a" * 40,
        status=CandidateStatus.GENERATED,
    )


def test_context_plane_builds_candidate_block_with_source_refs(tmp_path):
    candidate_store = CandidateStore(tmp_path)
    candidate_store.save(_candidate_card())

    plane = GlobalContextPlane(
        tmp_path,
        {"task": {"name": "task", "goal": "improve metric"}},
        candidate_store=candidate_store,
        execution_store=ExecutionStore(tmp_path, event_store=EventStore(tmp_path)),
        event_store=EventStore(tmp_path),
        artifact_store=ArtifactStore(tmp_path),
        store=RunStore(tmp_path),
    )

    blocks = plane.candidate_blocks(["cand_001"])

    assert blocks[0].kind == ContextBlockKind.CANDIDATE_FACT
    assert blocks[0].entity_refs[0].entity_id == "cand_001"
    assert blocks[0].source_refs[0].source_type == "candidate"
    assert plane.entity_store.get("candidate", "cand_001") is not None


def test_context_plane_run_facts_are_deterministic(tmp_path):
    plane = GlobalContextPlane(
        tmp_path,
        {"task": {"name": "task", "goal": "improve metric"}, "budget": {"max_rounds": 3}},
        candidate_store=CandidateStore(tmp_path),
        execution_store=ExecutionStore(tmp_path),
        event_store=EventStore(tmp_path),
        artifact_store=ArtifactStore(tmp_path),
        store=RunStore(tmp_path),
    )
    state = LoopState(task_name="task", round_id=2)

    first = [block.to_dict() for block in plane.run_fact_blocks(state)]
    second = [block.to_dict() for block in plane.run_fact_blocks(state)]

    assert first == second
