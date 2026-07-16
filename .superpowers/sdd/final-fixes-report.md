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
