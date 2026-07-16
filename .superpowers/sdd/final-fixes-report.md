# Final whole-branch fixes report

## Status

All six Important findings in `final-review-findings.md` are addressed without
Meta, budget, draft, or production mutation. Draft 156 and the disabled-first
feature flag defaults are unchanged.

## TDD evidence

- Runtime/window RED: 4 focused failures exposed absent account-timezone and
  freshness settings, hardcoded current-day/lookback/grace behavior, and the
  verifier's coupled freshness/filter behavior. GREEN: the focused config,
  hourly, and verifier slice passed 12 tests.
- Learning RED: 3 unit failures showed that `eligible_learning` had no learning
  type, delivery-only gate, metric, or compared outcome. GREEN:
  `tests/test_learnings.py` passed 11 tests.
- Prompt RED: the Meta draft prompt omitted all seven relevant learning rows.
  GREEN: prompt/draft tests passed 9 tests with only the five newest exact
  channel/objective/language/audience matches and safe empty behavior.
- Daily integration initially exposed five legacy assertions tied to one
  generic learning and the old evidence ID. The production path now persists
  separate delivery/conversion decisions, configured thresholds, deterministic
  rate outcomes, account timezone, and UTC alignment; legacy frozen snapshots
  receive explicit derived upgrade metadata.

## Implemented contract

- `META_ACCOUNT_TIMEZONE` defaults to `Europe/Brussels`; Insights stop at the
  last completed account day and use configured lookback. Stored snapshots
  include account-calendar bounds plus exact UTC interval/overlap days.
- Runtime uses the configured lookback, no-delivery grace, and all three
  learning thresholds. Delivery requires impressions and Meta LPV; conversion
  additionally requires registrations. Outcomes identify deterministic winner,
  loser, metric values, and absolute difference.
- Meta generation reads at most five newest learning texts using exact SQL
  dimension equality. Prompt text is individually capped; no-learning is safe.
  Scoring, persistence, Slack notification, and founder approval are unchanged.
- Verifier freshness uses dedicated
  `PERFORMANCE_SNAPSHOT_MAX_AGE_HOURS=2` and attribution counts include only
  exact `utm_content=draft-<draft_id>` rows.
- Both new non-secret controls are deployed through repository variables and
  written to the service environment. README documents the defaults.

## Verification

- Focused integration: 99 passed before the final assertion modernization;
  subsequent focused run passed 108 and exposed one expected dimensions
  assertion, which was updated to include UTC identity.
- Full suite: `451 passed in 52.07s` against PostgreSQL on port 55432.
- `uv run ruff check src tests`: passed after three import-order autofixes.
- `uv run ruff format --check src tests`: 100 files formatted.
- `git diff --check`: clean.

No push, deployment, production read, or production write was performed.

## Final tie and window-label follow-up

Strict TDD covered the last Important and Minor findings:

- RED: the pure decision returned an eligible directional winner for equal
  metric rates; the daily layer persisted two directional learning rows; the
  retrieval path had no defensive eligibility filter; and Slack labeled Meta
  account-calendar dates as UTC.
- GREEN: equal delivery or conversion metric values now return ineligible with
  reason `no_observed_difference` and no outcome. The daily persistence layer
  therefore creates or reinforces no reusable learning for a tie. Draft
  retrieval additionally requires persisted `decision.eligible=true` and a
  non-zero absolute difference, so a neutral/legacy row cannot enter the prompt.
- Non-tie ordering remains deterministic by metric value and publication ID.
- Daily summaries label account dates with the stored account timezone and
  show the stored half-open UTC interval explicitly.
- Focused pure, database, prompt, and formatting coverage passed 67 tests.
- Full suite passed 454 tests in 53.00s; final defensive focused coverage
  passed 5 tests, with Ruff, format, and diff checks clean.

## Decimal-safe retrieval follow-up

- RED: 19 parser-matrix cases failed because the defensive filter was an SQL
  string-trim heuristic and no pure numeric decision function existed.
- GREEN: retrieval now reads at most the 25 newest exact-dimension candidates,
  parses `decision.outcome.absolute_difference` with `Decimal` in Python, and
  returns at most five prompt learnings.
- The filter requires JSON boolean `eligible=true`; rejects missing values,
  booleans, malformed text, NaN, positive/negative Infinity, and every tested
  mathematical zero representation (`0`, `0.00`, `-0.00`, `0e0`, `0E-10`);
  and accepts only finite, mathematically non-zero decimal values.
- Focused parser, bounded retrieval, and prompt coverage passed 21 tests.
- Full suite passed 473 tests in 53.90s; Ruff, format, and diff checks passed.
