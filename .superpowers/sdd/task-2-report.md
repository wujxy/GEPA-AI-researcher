# Task 2: Lightweight Entity Store

## Status

DONE

## Implementation

- Added `EntityRecord` with `SourceRef` serialization and deserialization.
- Added `EntityStore` with canonical latest-record JSON files at `context/entities/<entity_type>/<entity_id>.json`.
- Added append-only audit entries at `context/entities.jsonl` for every upsert.
- Added `get`, deterministic `list_by_type`, and deterministic `list_all` operations.
- Used the existing storage IO helpers and an `RLock`, matching neighboring stores.

## TDD Evidence

1. Added `tests/test_entity_store.py` before the implementation.
2. Initial focused run failed during collection with:

   `ModuleNotFoundError: No module named 'gepa_researcher.context.entity_store'`

3. Implemented the minimal store.
4. Focused run passed: `python -m pytest tests/test_entity_store.py -q` -> `1 passed`.

## Regression Verification

`python -m pytest tests/test_entity_store.py tests/test_context_blocks.py tests/test_context_views.py -q` -> `14 passed`.

## Files Changed

- `gepa_researcher/context/entity_store.py`
- `tests/test_entity_store.py`
