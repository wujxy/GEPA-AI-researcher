from gepa_researcher.context.blocks import ContextRole
from gepa_researcher.context.plane import GlobalContextPlane
from gepa_researcher.context.views import ContextViewBuilder
from gepa_researcher.domain.candidate import CandidateCard, CandidateStatus, ProposalIdea
from gepa_researcher.models.schemas import Candidate, LoopState, ParetoFrontier, SampleTrace, Trace
from gepa_researcher.storage.artifact_store import ArtifactStore
from gepa_researcher.storage.candidate_store import CandidateStore
from gepa_researcher.storage.event_store import EventStore
from gepa_researcher.storage.execution_store import ExecutionStore
from gepa_researcher.storage.store import RunStore


def _plane(tmp_path):
    candidate_store = CandidateStore(tmp_path)
    candidate_store.save(
        CandidateCard(
            candidate_id="cand_001",
            round_id=1,
            parent_candidate_ids=(),
            proposal_id="cand_001",
            proposal=ProposalIdea(
                proposal_id="cand_001",
                hypothesis="h",
                scope="scope",
                proposed_change="change",
                rationale="rationale",
                expected_improvement="improve",
                risk="risk",
                prompt_text="prompt",
            ),
            base_revision="a" * 40,
            status=CandidateStatus.MATERIALIZED,
            result_revision="b" * 40,
            score_summary={"primary": 0.7},
        )
    )
    return GlobalContextPlane(
        tmp_path,
        {"task": {"name": "task", "goal": "goal"}, "run_id": "run-1"},
        candidate_store=candidate_store,
        execution_store=ExecutionStore(tmp_path),
        event_store=EventStore(tmp_path),
        artifact_store=ArtifactStore(tmp_path),
        store=RunStore(tmp_path),
    )


def _candidate(expected_gain=None):
    return Candidate(
        candidate_id="cand_001",
        round_id=1,
        hypothesis="h",
        scope="scope",
        proposed_change="change",
        rationale="rationale",
        expected_improvement="improve",
        risk="risk",
        prompt_text="prompt",
        created_at="now",
        expected_gain=expected_gain,
    )


def test_proposer_view_is_deterministic_and_agent_visible(tmp_path):
    builder = ContextViewBuilder(_plane(tmp_path))
    state = LoopState(task_name="task", round_id=2, best_candidate_id="cand_001")

    first = builder.for_proposer(state, parent_ids=["cand_001"]).to_dict()
    second = builder.for_proposer(state, parent_ids=["cand_001"]).to_dict()

    assert first == second
    assert first["role"] == ContextRole.PROPOSER.value
    assert all(block["visibility"] in {"agent", "user"} for block in first["blocks"])
    assert any(block["inline_content"].get("score_summary") == {"primary": 0.7} for block in first["blocks"])


def test_judge_view_excludes_proposer_authored_anchoring_fields(tmp_path):
    builder = ContextViewBuilder(_plane(tmp_path))
    trace = Trace(
        candidate_id="cand_001",
        round_id=1,
        samples=[SampleTrace(sample_id="s1", input="in", output="out", expected="exp", logs="logs")],
    )

    view_text = str(builder.for_judge(_candidate(expected_gain=999.0), trace, {"_eval_phase": "pareto"}).to_dict())

    assert "999.0" not in view_text
    assert "expected_gain" not in view_text
    assert "expected_improvement" not in view_text
    assert "rationale" not in view_text
    assert "prompt_text" not in view_text


def test_proposer_view_accepts_a_pareto_frontier_model(tmp_path):
    builder = ContextViewBuilder(_plane(tmp_path))
    view = builder.for_proposer(
        LoopState(task_name="task", round_id=2),
        frontier=ParetoFrontier(round_id=1, candidate_ids=["cand_001"], per_task_best={}),
    ).to_dict()

    assert any(block["block_id"] == "candidate:cand_001" for block in view["blocks"])
    assert view["metadata"]["frontier"]["candidate_ids"] == ["cand_001"]


def test_proposer_view_preserves_parent_order_and_separates_loop_state(tmp_path):
    builder = ContextViewBuilder(_plane(tmp_path))

    view = builder.for_proposer(
        LoopState(task_name="task", round_id=2),
        parent_ids=["cand_002", "cand_001", "cand_002"],
    ).to_dict()

    assert view["metadata"]["parent_ids"] == ["cand_002", "cand_001"]
    run_blocks = [block for block in view["blocks"] if block["kind"] == "run_fact"]
    assert len(run_blocks) == 1
    assert "loop_state" not in run_blocks[0]["inline_content"]
    assert any(block["kind"] == "loop_state" for block in view["blocks"])
