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


def test_context_block_requires_source_or_explicit_system_kind():
    try:
        ContextBlock(
            block_id="block-2",
            kind=ContextBlockKind.CANDIDATE_FACT,
            title="Bad",
            summary=None,
            inline_content={},
            source_refs=[],
            entity_refs=[],
        )
    except ValueError as exc:
        assert "source_refs" in str(exc)
    else:
        raise AssertionError("expected missing provenance to fail")
