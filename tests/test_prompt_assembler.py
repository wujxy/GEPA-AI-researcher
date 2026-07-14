from gepa_researcher.context.blocks import (
    ContextBlock,
    ContextBlockKind,
    ContextRole,
    ContextVisibility,
    SourceRef,
)
from gepa_researcher.context.prompt_assembler import PromptAssembler
from gepa_researcher.context.views import ContextView
from gepa_researcher.models.schemas import ContextEnvelope, LoopState


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
