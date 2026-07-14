from __future__ import annotations

from pathlib import Path

from gepa_researcher.domain.artifact import ArtifactKind
from gepa_researcher.domain.candidate import CandidateCard, CandidateStatus, ProposalIdea
from gepa_researcher.domain.execution import (
    CapabilityPolicy,
    ExecutionBudget,
    ExecutionFailure,
    ExecutionPhase,
    ExecutionStatus,
    ExecutionSpec,
)
from gepa_researcher.storage.artifact_store import ArtifactStore
from gepa_researcher.storage.candidate_store import CandidateStore
from gepa_researcher.storage.event_store import EventStore
from gepa_researcher.storage.execution_store import ExecutionStore


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


def _card(candidate_id: str, round_id: int = 1, parents: tuple[str, ...] = ("seed_000",)) -> CandidateCard:
    proposal = _proposal(candidate_id)
    return CandidateCard(
        candidate_id=candidate_id,
        round_id=round_id,
        parent_candidate_ids=parents,
        proposal_id=proposal.proposal_id,
        proposal=proposal,
        base_revision="a" * 40,
        status=CandidateStatus.ADMITTED,
    )


def _spec(execution_id: str, phase: ExecutionPhase) -> ExecutionSpec:
    return ExecutionSpec(
        execution_id=execution_id,
        run_id="run-001",
        round_id=1,
        candidate_id="cand_001_000",
        phase=phase,
        input_revision="a" * 40,
        dataset_ref=None,
        evaluator_version=None,
        budget=ExecutionBudget(wall_seconds=600),
        capability_policy=CapabilityPolicy(
            repo_writable=phase == ExecutionPhase.IMPLEMENTATION,
            network_allowed=False,
            allowed_tools=("bash", "git"),
            forbidden_paths=("tests/**",),
        ),
    )


def test_candidate_store_lists_rounds_and_children(tmp_path: Path):
    store = CandidateStore(tmp_path)
    child_a = _card("cand_001_000", round_id=1, parents=("seed_000",))
    child_b = _card("cand_001_001", round_id=1, parents=("seed_000", "seed_001"))
    other = _card("cand_002_000", round_id=2, parents=("cand_001_000",))

    store.save(child_a)
    store.save(child_b)
    store.save(other)

    assert store.get("cand_001_000").candidate_id == "cand_001_000"
    assert [card.candidate_id for card in store.list_by_round(1)] == ["cand_001_000", "cand_001_001"]
    assert [card.candidate_id for card in store.list_children("seed_000")] == ["cand_001_000", "cand_001_001"]
    assert (tmp_path / "candidates" / "cand_001_000.json").exists()
    assert len((tmp_path / "candidates.jsonl").read_text(encoding="utf-8").splitlines()) == 3


def test_execution_store_keeps_multiple_records_for_one_candidate(tmp_path: Path):
    store = ExecutionStore(tmp_path)
    record_a = store.create_pending(_spec("exec-a", ExecutionPhase.IMPLEMENTATION))
    record_b = store.create_pending(_spec("exec-b", ExecutionPhase.FEEDBACK_EVAL))

    record_a.status = ExecutionStatus.SUCCEEDED
    record_a.result_revision = "b" * 40
    store.save(record_a)
    record_b.status = ExecutionStatus.RUNNING
    store.save(record_b)

    assert store.get("exec-a").execution_id == "exec-a"
    assert [record.execution_id for record in store.list_for_candidate("cand_001_000")] == ["exec-a", "exec-b"]
    assert [record.execution_id for record in store.list_by_phase("cand_001_000", ExecutionPhase.FEEDBACK_EVAL)] == ["exec-b"]
    assert [record.execution_id for record in store.list_active()] == ["exec-b"]
    assert len((tmp_path / "executions.jsonl").read_text(encoding="utf-8").splitlines()) == 4


def test_execution_store_rejects_illegal_status_transitions(tmp_path: Path):
    store = ExecutionStore(tmp_path)
    store.create_pending(_spec("exec-a", ExecutionPhase.IMPLEMENTATION))

    try:
        store.mark_running("exec-a")
    except ValueError as exc:
        assert "illegal execution status transition" in str(exc)
    else:
        raise AssertionError("mark_running should reject pending -> running")

    store.mark_preparing("exec-a")
    store.mark_running("exec-a")
    store.mark_collecting("exec-a")
    store.mark_succeeded("exec-a", result_revision="b" * 40)

    try:
        store.mark_failed("exec-a", ExecutionFailure(code="AGENT_PROCESS_FAILED", message="late failure"))
    except ValueError as exc:
        assert "terminal" in str(exc)
    else:
        raise AssertionError("mark_failed should reject succeeded -> failed")


def test_execution_store_marks_active_records_interrupted(tmp_path: Path):
    store = ExecutionStore(tmp_path)
    store.create_pending(_spec("exec-pending", ExecutionPhase.IMPLEMENTATION))
    store.create_pending(_spec("exec-running", ExecutionPhase.FEEDBACK_EVAL))
    store.mark_preparing("exec-running")
    store.mark_running("exec-running")

    interrupted = store.mark_active_interrupted("run restarted")

    assert [record.execution_id for record in interrupted] == ["exec-pending", "exec-running"]
    assert store.get("exec-pending").failure.code == "RUN_INTERRUPTED"
    assert store.get("exec-pending").failure.retryable is True
    assert store.get("exec-running").status == ExecutionStatus.FAILED


def test_artifact_store_copies_files_under_execution_root(tmp_path: Path):
    source = tmp_path / "source-report.txt"
    source.write_text("metric=1.0\n", encoding="utf-8")
    store = ArtifactStore(tmp_path)

    ref = store.put("exec-a", ArtifactKind.METRICS, source)

    assert ref.execution_id == "exec-a"
    assert ref.kind == ArtifactKind.METRICS
    assert ref.path == "artifacts/exec-a/source-report.txt"
    assert ref.size_bytes == len("metric=1.0\n")
    assert (tmp_path / ref.path).read_text(encoding="utf-8") == "metric=1.0\n"
    assert len((tmp_path / "artifacts.jsonl").read_text(encoding="utf-8").splitlines()) == 1


def test_artifact_store_indexes_json_artifacts_by_execution_and_kind(tmp_path: Path):
    store = ArtifactStore(tmp_path)

    ref = store.put_json(
        "exec-a",
        ArtifactKind.EXECUTION_TRACE,
        "trace.json",
        {"candidate_id": "cand_001_000", "samples": []},
        metadata={"phase": "feedback_eval"},
    )

    assert store.get(ref.artifact_id).artifact_id == ref.artifact_id
    assert [item.artifact_id for item in store.list_for_execution("exec-a")] == [ref.artifact_id]
    assert [item.artifact_id for item in store.list_by_kind(ArtifactKind.EXECUTION_TRACE)] == [ref.artifact_id]
    assert (tmp_path / ref.path).read_text(encoding="utf-8").startswith("{")


def test_event_store_appends_and_filters_typed_events(tmp_path: Path):
    events = EventStore(tmp_path)

    event = events.append(
        event_type="execution.status_changed",
        source="execution_store",
        run_id="run-001",
        round_id=1,
        candidate_id="cand_001_000",
        execution_id="exec-a",
        payload={"from": "running", "to": "collecting"},
    )

    assert event.sequence == 1
    assert events.get(event.event_id).event_type == "execution.status_changed"
    assert [item.event_id for item in events.list_for_candidate("cand_001_000")] == [event.event_id]
    assert [item.event_id for item in events.list_for_execution("exec-a")] == [event.event_id]


def test_execution_store_emits_events_when_event_store_is_configured(tmp_path: Path):
    events = EventStore(tmp_path)
    store = ExecutionStore(tmp_path, event_store=events)

    store.create_pending(_spec("exec-a", ExecutionPhase.IMPLEMENTATION))
    store.mark_preparing("exec-a")

    event_types = [event.event_type for event in events.list_all()]
    assert event_types == ["execution.created", "execution.status_changed"]
