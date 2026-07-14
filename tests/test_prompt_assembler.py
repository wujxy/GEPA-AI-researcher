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
