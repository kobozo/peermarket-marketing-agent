# Task 2 report: Read-only Meta Insights client

## Status

Complete. Implemented and committed the read-only Meta Insights adapter without pushing or touching production.

Commit: `3f2d7e7 feat: collect normalized Meta Insights`

## TDD evidence

- RED: `uv run pytest -q tests/test_meta_insights.py` failed during collection with the expected `ModuleNotFoundError: No module named 'peermarket_agent.meta_insights'`.
- GREEN: after the minimum implementation, the focused suite reported `8 passed in 0.11s`.
- Post-format GREEN: the focused suite again reported `8 passed in 0.11s`.

## Implemented contract

- Frozen `MetaInsightSnapshot` with exact date window and UTC `retrieved_at`.
- `async fetch_meta_insights(...)` delegates Meta SDK initialization, the read-only `Ad.get_insights` call, and paginated cursor consumption to `asyncio.to_thread`.
- Exact delivery fields requested; paginated counters and action arrays normalized and summed.
- Missing fields/actions normalize to zero; ratio fields use denominator guards.
- Spend and derived currency metrics use `Decimal` and half-up cent rounding.
- Transient/rate-limit failures use bounded exponential backoff with at most three attempts.
- Permanent failures are not retried.
- `MetaInsightsError.transient` is exposed while messages contain only sanitized code/type/status metadata, never raw SDK messages or credentials.
- No Meta mutation API is imported or called.

## Tests

SDK-boundary mocks prove:

- missing-field/action and Decimal normalization;
- multi-page aggregation and per-action summing;
- exact fields, date window, UTC retrieval timestamp, and frozen snapshot;
- transient recovery on the third attempt;
- rate-limit exhaustion at exactly three attempts;
- permanent no-retry and credential redaction;
- denominator guards for ratios;
- rejection of attempt counts above the hard bound before SDK access.

## Verification

- `uv run pytest -q tests/test_meta_insights.py` -> `8 passed`
- `uv run ruff format --check src/peermarket_agent/meta_insights.py tests/test_meta_insights.py` -> 2 files already formatted
- `uv run ruff check src/peermarket_agent/meta_insights.py tests/test_meta_insights.py` -> all checks passed
- `git diff --check` -> clean
- staged diff before commit contained exactly the two requested source/test files

## Self-review

No blocking findings. Scope is restricted to collection/normalization, credentials are passed only to SDK initialization, cursor iteration remains off the event loop, and retry classification does not retry permanent permission/configuration errors.

## Important review findings follow-up

All three Important findings were fixed with regression-first TDD.

### RED evidence

After adding the three regression cases, `uv run pytest -q tests/test_meta_insights.py` reported `3 failed, 7 passed`:

- caller mutation changed `snapshot.actions`;
- deterministic concurrent initialization cross-bound the first ad to the second token through the SDK global default;
- attacker-controlled `api_error_type` content surfaced credentials in `MetaInsightsError`.

### Fixes

- `MetaInsightSnapshot.actions` is now an immutable `Mapping`; `__post_init__` takes a defensive `dict` copy and wraps it in `MappingProxyType`.
- The worker captures the concrete API returned by `FacebookAdsApi.init` and injects it into `Ad(ad_id, api=api)`. The installed SDK implementation was inspected to confirm `init` returns that API instance.
- Error messages now contain only a closed, normalized category and integer code. Arbitrary SDK type/message metadata is never interpolated.
- Sanitized errors are raised after leaving the `except` handler, preventing the credential-bearing source exception from being retained as `__context__`; tests also prove no `__cause__` remains.

### GREEN evidence

- Focused suite after implementation: `10 passed in 0.12s`.
- Fresh suite after formatting/import fixes: `10 passed in 0.13s`.
- Ruff format check: both files already formatted.
- Ruff lint: all checks passed.
- `git diff --check`: clean.

No API, production, push, or Meta mutation action was performed.
