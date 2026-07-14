from gepa_researcher.context.blocks import SourceRef
from gepa_researcher.context.presentation import PresentationStream


def test_presentation_stream_persists_user_facing_events(tmp_path):
    stream = PresentationStream(tmp_path)

    event = stream.append(
        event_type="candidate_failed",
        message="Candidate cand_001 failed implementation",
        level="warning",
        round_id=1,
        candidate_id="cand_001",
        source_refs=[SourceRef(source_type="candidate", source_id="cand_001")],
    )

    restored = stream.list_all()[0]
    assert restored.event_id == event.event_id
    assert restored.message == "Candidate cand_001 failed implementation"
    assert restored.source_refs[0].source_id == "cand_001"
