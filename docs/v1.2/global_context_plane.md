# GEPA v1.2 Global Context Plane

## What Changed

- Agent prompts are assembled from ContextView objects.
- `_gepa_context` is no longer the primary context transport.
- Context facts are traceable through SourceRef and EntityRef.
- FileCache keys include repo id, commit sha, path, and content hash.
- Prompt trimming affects only prompt rendering, not stored context.

## Current Limits

- Planner and Critic are not introduced in v1.2.
- Embeddings and vector retrieval are not introduced in v1.2.
- Compact agent is not introduced in v1.2.
- DerivedSummary blocks are schema-ready but not automatically generated.

## Operational Notes

- Existing canonical task config is unchanged.
- Resume reconstructs context views from persisted stores.
- User-facing presentation events are written to `presentation_events.jsonl`.
