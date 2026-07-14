import pytest

from gepa_researcher.context.blocks import (
    ContextBlock,
    ContextBlockKind,
    ContextRole,
    ContextVisibility,
    EntityRef,
    SourceRef,
)


def test_context_block_round_trips_with_provenance():
    block = ContextBlock(
        block_id="block-1",
        kind=ContextBlockKind.CANDIDATE_FACT,
        title="Candidate cand_001",
        summary="Small mutation",
        inline_content={"candidate_id": "cand_001"},
        source_refs=[SourceRef(source_type="candidate", source_id="cand_001", field="candidate_id")],
        entity_refs=[EntityRef(entity_type="candidate", entity_id="cand_001")],
        role_scope=[ContextRole.PROPOSER, ContextRole.JUDGE],
        visibility=ContextVisibility.AGENT,
        schema_version="context-block-v1",
    )

    restored = ContextBlock.from_dict(block.to_dict())

    assert restored == block
    assert restored.source_refs[0].source_type == "candidate"
    assert restored.entity_refs[0].entity_id == "cand_001"


def test_context_block_serialization_detaches_nested_inline_content():
    block = ContextBlock(
        block_id="block-serialized",
        kind=ContextBlockKind.RUN_FACT,
        title="Run facts",
        summary=None,
        inline_content={"nested": {"value": "original"}},
        source_refs=[],
        entity_refs=[],
    )

    serialized = block.to_dict()
    serialized["inline_content"]["nested"]["value"] = "mutated"

    assert block.inline_content == {"nested": {"value": "original"}}


@pytest.mark.parametrize(
    "kind",
    [
        ContextBlockKind.EXECUTION_FACT,
        ContextBlockKind.USER_EVENT,
        ContextBlockKind.ARTIFACT_REF,
        ContextBlockKind.FILE_CONTEXT,
    ],
)
def test_context_block_requires_provenance_for_derived_kinds(kind):
    with pytest.raises(ValueError, match="source_refs"):
        ContextBlock(
            block_id="block-2",
            kind=kind,
            title="Bad",
            summary=None,
            inline_content={},
            source_refs=[],
            entity_refs=[],
        )
