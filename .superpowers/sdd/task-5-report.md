# Task 5 Report: CI Configuration and Activation Guard

## Implemented

- Added `Settings.meta_auto_activate` with the safe default `False` and standard
  Pydantic environment boolean parsing.
- Added an early pipeline refusal for approved Meta drafts while automatic
  activation is disabled. The guard runs before screenshots, resource creation,
  publication persistence, or activation, and alerts the founder.
- Updated direct pipeline tests to opt into activation intentionally.
- Added deployment workflow propagation from the non-secret GitHub repository
  variable, falling back to `false`, without printing the environment file.
- Added a disposable `pgvector/pgvector:pg15` test job. Deployment now depends on
  the full pytest suite succeeding in that job.

## TDD Evidence

- RED: focused tests reported five configuration failures because the field was
  absent; the database-backed pipeline test proceeded into Meta creation while
  disabled.
- GREEN: the same focused slice passed: `10 passed`.

## Verification

- Full suite with disposable local pgvector endpoint: `188 passed in 13.34s`.
- `uv run ruff check src tests`: passed.
- Ruff format check for all Task 5 touched Python files: passed.
- Workflow YAML syntax checks for `ci.yml` and `deploy.yml`: passed.
- `git diff --check`: passed.

## Existing Concern

The repository-wide `uv run ruff format --check src tests` still reports three
pre-existing files from earlier feature tasks (`publications.py`,
`test_migrations.py`, and `test_publications.py`). Task 5 did not modify those
unrelated files; all Python files changed by Task 5 satisfy the formatter.

No repository variable was changed, no branch was pushed, and no deployment or
production reconciliation was attempted.

## Review Blocker Follow-up

The three previously reported formatter failures were resolved by running Ruff
format only on `src/peermarket_agent/publications.py`,
`tests/test_migrations.py`, and `tests/test_publications.py`. Diff inspection
confirmed mechanical wrapping, method-chain layout, and equivalent string quote
changes only; behavior and SQL content are unchanged.

Fresh post-format verification:

- `uv run ruff format --check src tests`: `70 files already formatted`.
- `uv run ruff check src tests`: passed.
- Full suite with the local disposable pgvector DSN: `188 passed in 13.84s`.
- Both workflow YAML files parsed successfully and deployment contract
  assertions passed.
- `git diff --check`: passed.

## Meta attribution-learning Task 5 review follow-up (2026-07-16)

The daily attribution implementation was hardened in a strict review-driven
TDD pass. No Meta resource, budget ledger, deployment, production database, or
external configuration was mutated.

### Root causes and RED evidence

- The initial evaluator covered only impressions, Meta landing-page views, and
  a legacy registration event name. It therefore omitted the designed funnel,
  costs, and denominator guards. The expanded regressions failed across the
  exact metric map and every missing/zero denominator.
- Comparison identity used fabricated objective, audience, and `utc-day`
  defaults, and invalid source windows fell back to the current date. Missing
  dimension and missing/invalid source-window regressions failed.
- Learning evidence retained aggregate IDs and totals but not complete
  per-variant values, dimensions, thresholds, or replayable decisions.
  Evidence-shape, reinforcement, and concurrent replay regressions failed.
- The hourly snapshot did not persist an explicit requested rolling-window
  definition, and new Meta drafts did not persist their fixed traffic
  objective. Both source-identity regressions failed.
- The first review RED run reported `32 failed, 19 passed`; the secondary
  evidence/source RED run reported `2 failed, 1 passed`.

### Implemented review contract

- `evaluate_publication` now returns the exact designed raw and derived metric
  set: approved budget, spend, delivery, impressions, clicks/link clicks, Meta
  and first-party landings, registrations, first listing created/published,
  identity verification, and all seven guarded cost/conversion calculations.
- Absent Meta values and suppressed/absent aggregate event groups remain
  `None`; Slack renders them as `unavailable`. A missing or zero denominator
  always yields `None`, never a fabricated zero conversion.
- Daily summaries include every designed metric, explicit source window and
  definition, sample sizes, and the Ads Manager link, under a descriptive-only
  heading with no causal claim.
- Completed immutable observations require valid explicit source
  `window_start`, `window_stop`, and `window_definition`. Missing, malformed,
  reversed, or incomplete source windows create no observation or learning and
  are reported as unavailable. The hourly collector records the actual
  requested rolling inclusive-calendar-day identity. Same-day start/stop
  bounds remain a valid one-day inclusive window; only reversed bounds fail.
- Reusable comparisons reject any missing or blank channel, objective, language,
  audience, window definition, or bounds. They compare exact definitions and
  bounds; persisted evidence also records inclusive window length.
- New Meta drafts persist the connector's actual `OUTCOME_TRAFFIC` objective at
  source. The daily layer does not infer missing objectives or audiences.
- Each eligible learning evidence run records a deterministic decision ID,
  eligible/reason result, dimensions, exact window, thresholds, aggregate
  sample, and per-variant publication ID, immutable evidence ID, complete
  compared metric values, and threshold sample sizes.
- Replays of the same decision are idempotent. A genuinely new comparable
  window reinforces once while retaining all prior replayable evidence runs.
  Publication row locks serialize concurrent daily replays.

### Review verification

- Focused RED-to-GREEN suite:
  `tests/test_performance_daily.py tests/test_learnings.py tests/test_agent_hourly_loop.py tests/test_cli_draft.py`
  -> `60 passed`.
- Focused plus adjacent database suite:
  `tests/test_performance_daily.py tests/test_learnings.py tests/test_agent_hourly_loop.py tests/test_cli_draft.py tests/test_agent_main.py tests/test_migrations.py tests/test_publications.py tests/test_performance.py tests/test_meta_insights.py`
  -> `121 passed in 13.98s` against local PostgreSQL on port 55432.
- `uv run ruff check src tests` -> all checks passed.
- `uv run ruff format --check src tests` -> 98 files already formatted.
- `git diff --check` -> clean.

## Durable daily-summary delivery follow-up (2026-07-16)

### RED evidence

The durable-delivery regressions initially reported `7 failed, 36 passed`:

- no daily summary outbox table existed;
- a false notifier result was discarded rather than retained for retry;
- notifier exceptions escaped and could expose their raw message;
- a newer daily window superseded an older failed send;
- concurrent runs sent the same summary twice;
- successful replays sent again; and
- stale claims had no durable retry mechanism.

A separate recovery regression then failed because an immutable observation
created before the outbox migration was not backfilled into a pending summary.

### Implemented delivery contract

- Added the idempotent `daily_performance_summary_outbox` migration with a
  unique immutable summary key, exact source-window identity, publication and
  evidence references, sanitized message, pending/sent status, attempt and sent
  timestamps, claim token/lease, and bounded failure category.
- Summary rows are inserted in the same transaction as immutable observations
  and learnings, before any Slack call. Existing observations are also
  idempotently reconstructed into missing outbox rows during rollout/recovery.
- The drain claims only the oldest pending window under a row lock. An active
  oldest lease prevents a concurrent worker from overtaking it; an expired
  lease is reclaimed. Newer windows drain only after all older sends are
  confirmed.
- Slack delivery occurs outside the database transaction. A truthy result marks
  the token-matched row sent atomically. False or exception releases the claim,
  leaves the row pending, records only `notification_not_confirmed` or
  `notification_exception`, and stops the drain so immediate retries cannot
  loop or reorder windows.
- Sent summaries and concurrent runs are idempotent. No Meta resource, budget,
  production system, PII, credential, or raw exception text is read or mutated
  by this path.

### Focused GREEN

`tests/test_performance_daily.py tests/test_learnings.py tests/test_migrations.py tests/test_agent_main.py`
reported `54 passed in 6.97s` against local PostgreSQL on port 55432.

Final focused-plus-adjacent verification reported `128 passed in 16.33s`;
repository-wide Ruff check passed, all 98 Python files were already formatted,
and `git diff --check` was clean.
