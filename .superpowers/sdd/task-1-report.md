# Task 1: Context Block Domain Model

## Status

Implemented the GEPA v1.2 context block domain model in the scoped files.

## Changes

- Added the four required string enums for roles, block kinds, visibility, and render modes.
- Added frozen `SourceRef` and `EntityRef` dataclasses with dictionary serialization.
- Added frozen `ContextBlock` with nested reference serialization and enum reconstruction.
- Enforced provenance for every block kind except `RUN_FACT`.
- Exported the public context model from `gepa_researcher.context`.
- Added focused round-trip and provenance validation tests.

## TDD Evidence

- Red: `python -m pytest tests/test_context_blocks.py -q` failed during collection with `ModuleNotFoundError: No module named 'gepa_researcher.context'`.
- Green: `python -m pytest tests/test_context_blocks.py -q` passed: `2 passed`.

## Verification

- Focused tests: `2 passed`.
- Full suite: `192 passed, 5 skipped, 1 failed`.

The single full-suite failure is unrelated to this task. `tests/test_agent_client.py::ClaudeCodeClientTest::test_run_json_supports_command_prefix_clean_env_and_container_command` launches a temporary Python script, and the environment's Python executable cannot load `libpython3.11.so.1.0` when invoked with the test's clean environment.
