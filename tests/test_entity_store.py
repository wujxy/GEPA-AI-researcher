from gepa_researcher.context.blocks import SourceRef
from gepa_researcher.context.entity_store import EntityRecord, EntityStore


def test_entity_store_upserts_and_lists_by_type(tmp_path):
    store = EntityStore(tmp_path)
    first = EntityRecord(
        entity_type="candidate",
        entity_id="cand_001",
        summary="first",
        source_refs=[SourceRef(source_type="candidate", source_id="cand_001")],
        metadata={"status": "generated"},
    )
    second = EntityRecord(
        entity_type="candidate",
        entity_id="cand_001",
        summary="updated",
        source_refs=[SourceRef(source_type="candidate", source_id="cand_001")],
        metadata={"status": "accepted"},
    )

    store.upsert(first)
    store.upsert(second)

    restored = store.get("candidate", "cand_001")
    assert restored.summary == "updated"
    assert restored.metadata["status"] == "accepted"
    assert [item.entity_id for item in store.list_by_type("candidate")] == ["cand_001"]
