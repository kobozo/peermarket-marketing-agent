# Task 3 report: Performance snapshots and delivery classification

## Status

Complete. Implemented deterministic performance derivation, delivery classification, and atomic publication performance persistence. Review findings were fixed in a second strict-TDD pass. No push, production access, scheduler change, alert change, or Meta API mutation was performed.

Initial commit: `137eed0 feat: persist Meta delivery performance`

## Initial TDD evidence

- RED: the focused suite failed during collection because `peermarket_agent.performance` and `save_performance_snapshot` did not exist.
- GREEN: `tests/test_performance.py tests/test_publications.py` reported `34 passed` against the disposable PostgreSQL database.
- A second RED/GREEN cycle proved partial fields within an existing namespace were initially replaced, then retained after the merge fix.

## Implemented contract

- `derive_performance` retains current/previous snapshots, derives non-negative metric deltas, and marks Meta restatements.
- `classify_delivery` deterministically distinguishes healthy, reviewing, no-delivery, rejected/error, terminal, and unknown states.
- Grace-period comparisons require timezone-aware timestamps and correctly compare different offsets.
- A configured-active hierarchy is unknown when any effective status is missing or empty.
- `save_performance_snapshot` rejects absent publications and locks the publication row with `SELECT ... FOR UPDATE`.
- Performance updates recursively merge dictionaries at every depth; lists and scalar values replace prior values.
- Concurrent hourly/daily namespace updates therefore retain both namespaces and nested sibling fields.
- Persistence normalization recursively converts `Mapping`/`MappingProxyType` to dictionaries, tuples/lists to JSON arrays, `Decimal` to lossless strings, dates to ISO dates, and aware datetimes to UTC ISO timestamps.
- Unsupported values, non-string mapping keys, non-finite floats, and naive datetimes are rejected before database serialization.
- JSON serialization is deterministic (`sort_keys`, compact separators, and non-finite values disabled).
- The migration reasserts the JSONB default, repairs legacy null performance values, and enforces `NOT NULL`.

## Review-finding TDD evidence

The review regressions first reported `5 failed`:

- configured-active resources with missing/empty effective status were incorrectly classified healthy;
- nested `meta.latest` updates replaced sibling metrics;
- a real `MetaInsightSnapshot` payload failed on `date` serialization;
- unsupported objects leaked the generic encoder failure;
- the explicit nested tuple/list regression subsequently failed with `unsupported performance value: list` before its minimal fix.

After implementation, all individual regression tests passed.

The database round-trip test builds the payload from an actual `MetaInsightSnapshot` via `derive_performance`, including `Decimal`, `date`, aware `datetime`, and `MappingProxyType` values, persists it, and verifies the normalized JSONB values read back.

## Final verification

- `AGENT_DB_URL=postgresql+asyncpg://postgres:test@localhost:55432/agent_test uv run pytest -q tests/test_migrations.py tests/test_performance.py tests/test_publications.py tests/test_meta_insights.py` -> `59 passed in 5.15s`
- `uv run ruff check src tests` -> all checks passed
- `uv run ruff format --check src tests` -> 93 files already formatted
- `git diff --check` -> clean

## Scope review

Changes are restricted to performance derivation/classification, publication JSONB persistence, its idempotent migration hardening, and focused tests/reporting. No scheduler, notification, external API, deployment, or production state was changed.
