# Task 2 Report: Meta Activate, Verify, and Roll Back

## Status

Complete. Connector-level creation, activation, verification, and rollback primitives are implemented without adding activation orchestration to the pipeline.

## TDD evidence

### RED

- Added ordered activation and accepted-review-state tests first.
- Added activation-failure rollback and structured-error tests first.
- `uv run pytest tests/test_meta_ads.py -q` failed during collection because `MetaActivationResult` and the activation interfaces did not exist.
- During self-review, added a credential-redaction regression test. It failed because the rollback exception exposed the configured system-user token.

### GREEN

- Renamed the paused creation interface to `create_meta_ad_paused(...)` and mechanically updated its existing import/call sites.
- Added `MetaActivationResult`, preserving configured and effective status values separately for campaign, ad set, and ad.
- Added `activate_meta_ad(config, ids)` with campaign → ad set → ad activation ordering.
- Added `get_meta_ad_statuses(config, ids)` using explicit `status` and `effective_status` reads.
- Accepted `ACTIVE`, `IN_PROCESS`, and `PENDING_REVIEW` as valid effective states only when configured status is `ACTIVE`.
- Added `pause_meta_ad(config, ids)` with best-effort ad → ad set → campaign ordering.
- Activation failures now report phase, resource IDs, observed statuses, and rollback errors while excluding configured credentials.
- All synchronous Meta SDK operations remain inside `asyncio.to_thread` at the async boundary.
- Resource creation remains fully `PAUSED`; no pipeline activation/reconciliation orchestration was added.

## Self-review

- Confirmed no changes to `publications.py` or its Task 1 interfaces.
- Confirmed activation does not create or retry a hierarchy.
- Confirmed rollback continues after an individual pause failure.
- Confirmed verification rejects non-`ACTIVE` configured state and effective states outside the explicit allowlist.
- Confirmed the only pipeline change is the required paused-creator symbol rename; behavior remains unchanged.
- Added token redaction after identifying that SDK rollback exception text could contain credentials.

## Verification

- `uv run pytest tests/test_meta_ads.py -q` → `14 passed`
- `uv run ruff check src/peermarket_agent/meta_ads.py src/peermarket_agent/meta_pipeline.py tests/test_meta_ads.py tests/test_meta_pipeline.py` → `All checks passed!`
- `AGENT_DB_URL=postgresql+asyncpg://postgres:test@localhost:55432/agent_test uv run pytest -q` → `148 passed`
- `git diff --check` → clean

## Concerns

None blocking. Pipeline-level reconciliation and activation invocation remain intentionally deferred to the later orchestration task.

## Security follow-up

### RED

- Added a regression whose activation SDK exception contained the configured system-user token and inspected both `MetaAdsError.__cause__` and the formatted chained traceback. Focused tests failed because the original `RuntimeError` remained attached as `__cause__`.
- Added a regression with two-character app secret and system-user token values embedded as standalone diagnostic values. Focused tests failed because the previous minimum-length guard left both credentials visible.

### GREEN

- Activation now raises its sanitized structured `MetaAdsError` with `from None`, suppressing the credential-bearing SDK exception from chained tracebacks and leaving no explicit cause.
- Added centralized credential redaction for every non-empty configured app secret and system-user token. Longer credentials use exact literal replacement; short credentials use token-boundary matching so standalone secret values are removed without corrupting ordinary words that merely contain the same characters.
- Focused connector tests now pass with 16 tests, including both security regressions.

### Verification

- `uv run pytest tests/test_meta_ads.py -q` → `16 passed`
- `uv run ruff check src/peermarket_agent/meta_ads.py src/peermarket_agent/meta_pipeline.py tests/test_meta_ads.py tests/test_meta_pipeline.py` → `All checks passed!`
- `AGENT_DB_URL=postgresql+asyncpg://postgres:test@localhost:55432/agent_test uv run pytest -q` → `150 passed`
- `git diff --check` → clean

## Rollback setup follow-up

### RED

- Added a regression where ad-set activation fails, status observation succeeds, and rollback API initialization then raises a diagnostic containing the system-user token.
- Focused tests failed because the rollback initialization exception escaped, replaced the required structured activation `MetaAdsError`, retained implicit exception context, and exposed the credential.

### GREEN

- Rollback credential/API initialization is now contained as a sanitized `rollback_errors["setup"]` entry, preserving the original activation phase, IDs, and observed statuses.
- Resource construction and pause updates now execute independently in ad → ad set → campaign order, so a constructor or update failure is sanitized and does not prevent remaining rollback attempts.
- The final structured activation error remains raised with suppressed chaining, so rollback setup diagnostics cannot reintroduce a credential-bearing cause or traceback.

### Verification

- `uv run pytest tests/test_meta_ads.py -q` → `17 passed`
- `uv run ruff check src/peermarket_agent/meta_ads.py src/peermarket_agent/meta_pipeline.py tests/test_meta_ads.py tests/test_meta_pipeline.py` → `All checks passed!`
- `AGENT_DB_URL=postgresql+asyncpg://postgres:test@localhost:55432/agent_test uv run pytest -q` → `151 passed`
- `git diff --check` → clean
