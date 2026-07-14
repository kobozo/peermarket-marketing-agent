# Task 4 Report: Safe Meta Draft Reconciliation CLI

## Result

- Added `peermarket-meta reconcile-draft` with required draft, campaign, ad-set,
  creative, and ad IDs.
- Added a read-only `--dry-run` that reports durable publication state and
  observed statuses without writing or dispatching activation.
- Refuses reconciliation when any supplied resource ID conflicts with durable
  state.
- Persists missing IDs inside `process_approved_meta_draft` while holding its
  per-draft advisory lock; status changes remain in the existing internal path.
- Validates draft existence, Meta type, and approval eligibility before writing.
- Does not overwrite a complete matching publication, preserving active state
  on harmless retries.
- Draft IDs are generic; draft 156 is an operational argument, not hardcoded.

## TDD evidence

- RED: `uv run pytest tests/test_cli_meta.py -q` failed during collection with
  `ModuleNotFoundError: peermarket_agent.cli_meta`.
- GREEN: focused mock-backed contract initially passed 5 tests.
- Regression RED: the active-publication preservation test failed because the
  wrapper downgraded the durable state; conditional upsert fixed it.
- Final focused run with disposable DSN:
  `AGENT_DB_URL=postgresql+asyncpg://postgres:test@localhost:55432/agent_test uv run pytest tests/test_cli_meta.py -q`
  -> focused CLI and pipeline verification `22 passed`.

## Verification

- Final full suite with disposable DSN -> `169 passed`.
- `uv run ruff check .` -> clean.
- `git diff --check` -> clean.

## Notes

- The database-backed CLI test creates a uniquely named schema and drops it in
  teardown. It never resets the shared `public` schema.
- A run without `AGENT_DB_URL` cannot execute the repository's DB-backed full
  suite; this is an environment prerequisite, not hidden by the task tests.
