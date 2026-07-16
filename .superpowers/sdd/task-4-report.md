# Task 4 Report: Hourly Meta Performance and Aggregate Attribution

## Result

- Added default-off `meta_insights_enabled` and
  `peermarket_attribution_enabled` feature flags.
- Added `PeermarketReadonly.fetch_attribution(start, stop)` with a fixed query
  against `marketing_attribution_daily` only. It never reads raw campaign
  touch/event tables.
- Added feature-gated hourly collection of Meta hierarchy status and Insights.
  It performs no Meta create, update, activate, pause, or delete operation.
- Preserved heartbeat and site KPI collection independently of the optional
  Meta job.
- Isolated Meta API and performance persistence failures by publication, with
  sanitized persisted diagnostics.
- Stored attribution availability and aggregate events only when attribution
  is enabled. Missing view/permission failures do not block Meta ingestion.

## Durable alert transitions

- Delivery-problem, recovery, and attribution-unavailable alerts use a durable
  claim token and `claimed_at` timestamp stored in the publication performance
  document.
- Claim acquisition locks the publication row and re-evaluates the latest
  delivered state and claim under that lock, rather than trusting the bulk
  publication snapshot.
- A concurrent collector observes the live claim and does not become a second
  sender.
- Notifier exceptions and false return values release the claim without
  advancing delivered state, leaving the transition immediately retryable.
- A truthy notifier result is followed by a row-locked, token-guarded finalize
  that records the delivered condition/observed state and clears the claim.
- Claims have a five-minute lease, so a process crash cannot suppress a
  transition permanently.
- Problem alerts deduplicate by publication, condition, and observed Meta
  state. A delivered problem receives one delivered recovery transition.

## TDD evidence

- Initial RED: focused tests failed during collection because
  `collect_meta_performance` did not exist.
- Initial GREEN: aggregate reader and hourly tests passed (`7 passed`).
- Concurrency follow-up RED reproduced six important failures:
  concurrent problem and attribution collectors each invoked the notifier
  twice, while false/exception delivery suppressed problem and attribution
  retries.
- Follow-up GREEN: real concurrent collector tests, problem/recovery/
  attribution false-and-exception retry tests, successful dedupe/recovery, and
  stale-lease reclamation all passed (`15 passed` in the hourly module).

## Verification

- Database: disposable local PostgreSQL on port 55432 using `agent_test`.
- Focused and adjacent run:
  `tests/test_attribution_reader.py tests/test_agent_hourly_loop.py
  tests/test_performance.py tests/test_agent_main.py` -> `38 passed`.
- Ruff on all Task 4 source and test files -> clean.
- `git diff --check` -> clean.
- No push, deployment, production database, or production Meta action was
  performed.
