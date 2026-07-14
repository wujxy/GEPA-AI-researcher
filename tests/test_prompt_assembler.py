from gepa_researcher.context.blocks import (
    ContextBlock,
    ContextBlockKind,
    ContextRenderMode,
    ContextRole,
    ContextVisibility,
    SourceRef,
)
from gepa_researcher.context.prompt_assembler import PromptAssembler
from gepa_researcher.context.views import ContextView
from gepa_researcher.models.schemas import Candidate, ContextEnvelope, LoopState, SampleTrace, Trace


def test_prompt_assembler_renders_stable_proposer_prompt():
    view = ContextView(
        role=ContextRole.PROPOSER,
        envelope=ContextEnvelope(role="proposer", round_id=1, phase="mutation", run_id="run-1"),
        blocks=[
            ContextBlock(
                block_id="run:task",
                kind=ContextBlockKind.RUN_FACT,
                title="Task",
                summary="Goal",
                inline_content={"goal": "improve metric"},
                source_refs=[],
                entity_refs=[],
                visibility=ContextVisibility.AGENT,
                role_scope=[ContextRole.PROPOSER],
            ),
            ContextBlock(
                block_id="candidate:cand_001",
                kind=ContextBlockKind.CANDIDATE_FACT,
                title="Parent",
                summary="parent summary",
                inline_content={"candidate_id": "cand_001"},
                source_refs=[SourceRef(source_type="candidate", source_id="cand_001")],
                entity_refs=[],
                visibility=ContextVisibility.AGENT,
                role_scope=[ContextRole.PROPOSER],
            ),
        ],
        metadata={"parent_ids": ["cand_001"]},
    )

    prompt = PromptAssembler().build_proposer_prompt(
        LoopState(task_name="task", round_id=1),
        {"task": {"goal": "improve metric"}, "runtime": {}, "evidence": {}},
        view,
        batch_size=1,
    )

    assert "You are the PROPOSER agent" in prompt
    assert "Context envelope" in prompt
    assert "parent summary" in prompt
    assert prompt == PromptAssembler().build_proposer_prompt(
        LoopState(task_name="task", round_id=1),
        {"task": {"goal": "improve metric"}, "runtime": {}, "evidence": {}},
        view,
        batch_size=1,
    )


def test_prompt_trimming_keeps_mandatory_blocks_and_uses_refs_for_overflow():
    blocks = [
        ContextBlock(
            block_id=f"candidate:cand_{i:03d}",
            kind=ContextBlockKind.CANDIDATE_FACT,
            title=f"Candidate {i}",
            summary=f"summary {i}",
            inline_content={"candidate_id": f"cand_{i:03d}", "details": "x" * 200},
            source_refs=[SourceRef(source_type="candidate", source_id=f"cand_{i:03d}")],
            entity_refs=[],
            visibility=ContextVisibility.AGENT,
            role_scope=[ContextRole.PROPOSER],
        )
        for i in range(5)
    ]
    view = ContextView(
        role=ContextRole.PROPOSER,
        envelope=ContextEnvelope(role="proposer", round_id=1, phase="mutation"),
        blocks=blocks,
        metadata={},
    )

    prompt = PromptAssembler(max_prompt_blocks=2).render_context_blocks(view)

    assert "summary 0" in prompt
    assert "summary 1" in prompt
    assert "candidate:cand_004" in prompt
    assert "omitted_context_refs" in prompt


def test_prompt_trimming_keeps_only_current_candidate_mandatory_for_executor():
    blocks = [
        ContextBlock(
            block_id="run:task",
            kind=ContextBlockKind.RUN_FACT,
            title="Task",
            summary="goal",
            inline_content={"goal": "improve"},
            source_refs=[],
            entity_refs=[],
            visibility=ContextVisibility.AGENT,
            role_scope=[ContextRole.EXECUTOR],
        ),
        *[
            ContextBlock(
                block_id=f"candidate:{candidate_id}",
                kind=ContextBlockKind.CANDIDATE_FACT,
                title=candidate_id,
                summary=candidate_id,
                inline_content={"candidate_id": candidate_id},
                source_refs=[SourceRef(source_type="candidate", source_id=candidate_id)],
                entity_refs=[],
                visibility=ContextVisibility.AGENT,
                role_scope=[ContextRole.EXECUTOR],
            )
            for candidate_id in ("historical", "current")
        ],
    ]
    view = ContextView(
        role=ContextRole.EXECUTOR,
        envelope=ContextEnvelope(role="executor", round_id=1, phase="execution", candidate_id="current"),
        blocks=blocks,
        metadata={},
    )

    prompt = PromptAssembler(max_prompt_blocks=1).render_context_blocks(view)

    assert '"goal": "improve"' in prompt
    assert "current" in prompt
    assert '"candidate:historical"]' in prompt


def test_prompt_budget_comes_from_assembler_not_context_config():
    blocks = [
        ContextBlock(
            block_id=f"candidate:cand_{index}",
            kind=ContextBlockKind.CANDIDATE_FACT,
            title=f"Candidate {index}",
            summary=f"summary {index}",
            inline_content={"candidate_id": f"cand_{index}"},
            source_refs=[SourceRef(source_type="candidate", source_id=f"cand_{index}")],
            entity_refs=[],
            visibility=ContextVisibility.AGENT,
            role_scope=[ContextRole.PROPOSER],
        )
        for index in range(3)
    ]
    view = ContextView(
        role=ContextRole.PROPOSER,
        envelope=ContextEnvelope(role="proposer", round_id=1, phase="mutation"),
        blocks=blocks,
        metadata={},
    )
    config = {
        "task": {"goal": "improve"},
        "runtime": {},
        "evidence": {},
        "context": {"max_prompt_blocks": 99},
    }

    prompt = PromptAssembler(max_prompt_blocks=1).build_proposer_prompt(
        LoopState(task_name="task", round_id=1), config, view
    )

    assert "summary 0" in prompt
    assert '"candidate:cand_2"' in prompt
    assert "omitted_context_refs" in prompt


def test_prompt_assembler_filters_blocks_by_visibility_and_role():
    blocks = [
        ContextBlock(
            block_id="run:task",
            kind=ContextBlockKind.RUN_FACT,
            title="Task",
            summary="goal",
            inline_content={"goal": "improve"},
            source_refs=[],
            entity_refs=[],
            visibility=ContextVisibility.AGENT,
            role_scope=[ContextRole.EXECUTOR],
        ),
        ContextBlock(
            block_id="candidate:judge-only",
            kind=ContextBlockKind.CANDIDATE_FACT,
            title="Judge only",
            summary="judge secret",
            inline_content={"candidate_id": "judge-only"},
            source_refs=[SourceRef(source_type="candidate", source_id="judge-only")],
            entity_refs=[],
            visibility=ContextVisibility.AGENT,
            role_scope=[ContextRole.JUDGE],
        ),
        ContextBlock(
            block_id="internal:note",
            kind=ContextBlockKind.DERIVED_SUMMARY,
            title="Internal",
            summary="internal secret",
            inline_content={"secret": True},
            source_refs=[SourceRef(source_type="internal", source_id="note")],
            entity_refs=[],
            visibility=ContextVisibility.INTERNAL,
            role_scope=[ContextRole.EXECUTOR],
        ),
    ]
    view = ContextView(
        role=ContextRole.EXECUTOR,
        envelope=ContextEnvelope(role="executor", round_id=1, phase="implementation"),
        blocks=blocks,
        metadata={},
    )

    prompt = PromptAssembler().render_context_blocks(view)

    assert "improve" in prompt
    assert "judge secret" not in prompt
    assert "internal secret" not in prompt


def test_prompt_assembler_rejects_mismatched_view_role():
    view = ContextView(
        role=ContextRole.JUDGE,
        envelope=ContextEnvelope(role="executor", round_id=1, phase="implementation"),
        blocks=[],
        metadata={},
    )

    try:
        PromptAssembler().render_context_blocks(view)
    except ValueError as exc:
        assert "role mismatch" in str(exc)
    else:
        raise AssertionError("expected role mismatch to be rejected")


def test_prompt_assembler_renders_actionable_artifact_refs():
    block = ContextBlock(
        block_id="artifact:plot",
        kind=ContextBlockKind.ARTIFACT_REF,
        title="Artifact: plot",
        summary="plot: artifacts/execution/plot.png",
        inline_content={
            "artifact_id": "plot",
            "execution_id": "exec-1",
            "kind": "plot",
            "path": "artifacts/execution/plot.png",
            "sha256": "abc",
            "size_bytes": 123,
        },
        source_refs=[SourceRef(source_type="artifact", source_id="plot", path="artifacts/execution/plot.png")],
        entity_refs=[],
        visibility=ContextVisibility.AGENT,
        role_scope=[ContextRole.JUDGE],
        render_mode=ContextRenderMode.REF,
    )
    view = ContextView(
        role=ContextRole.JUDGE,
        envelope=ContextEnvelope(role="judger", round_id=1, phase="pareto"),
        blocks=[block],
        metadata={},
    )

    prompt = PromptAssembler().render_context_blocks(view)

    assert "artifact_ref=artifact:plot" in prompt
    assert "path=artifacts/execution/plot.png" in prompt
    assert "plot: artifacts/execution/plot.png" in prompt


def test_executor_prompt_defines_metric_baseline_contract():
    candidate = Candidate(
        candidate_id="cand_001",
        round_id=1,
        hypothesis="h",
        scope="s",
        proposed_change="c",
        rationale="r",
        expected_improvement="faster primary metric",
        risk="low",
        prompt_text="prompt",
        created_at="now",
        parent_ids=[],
        target_files=["src/hot.py"],
    )
    view = ContextView(
        role=ContextRole.EXECUTOR,
        envelope=ContextEnvelope(role="executor", round_id=1, phase="implementation", candidate_id="cand_001"),
        blocks=[],
        metadata={},
    )

    prompt = PromptAssembler().build_executor_prompt(candidate, {"task": {"goal": "improve"}, "runtime": {}, "evidence": {}}, view)

    assert "metrics.baseline" in prompt
    assert "original configured baseline" in prompt
    assert "Do not put implementation-phase or previous evaluate_only measurements in metrics.baseline" in prompt


def test_judge_prompt_rejects_candidate_phase_metric_as_baseline_regression():
    candidate = Candidate(
        candidate_id="cand_001",
        round_id=1,
        hypothesis="h",
        scope="s",
        proposed_change="c",
        rationale="r",
        expected_improvement="faster primary metric",
        risk="low",
        prompt_text="prompt",
        created_at="now",
        parent_ids=[],
    )
    trace = Trace(
        candidate_id="cand_001",
        round_id=1,
        samples=[
            SampleTrace(
                sample_id="speed",
                input="",
                output="",
                expected="",
                logs="",
                artifacts={"metrics": {"primary": 0.608, "baseline": 0.603, "delta": 0.005}},
            )
        ],
    )
    view = ContextView(
        role=ContextRole.JUDGE,
        envelope=ContextEnvelope(role="judger", round_id=1, phase="pareto", candidate_id="cand_001"),
        blocks=[],
        metadata={},
    )

    prompt = PromptAssembler().build_judge_prompt(candidate, trace, {"task": {"goal": "improve"}}, view)

    assert "Do not treat implementation-phase metrics or earlier same-candidate measurements as the original baseline" in prompt
    assert "same-candidate remeasurement variance" in prompt
