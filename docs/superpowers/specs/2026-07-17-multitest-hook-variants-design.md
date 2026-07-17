# Multi-test setup: three multilingual hook variants

## Goal

Create the first controlled Jarvis experiment for campaign `120249125021520342` (draft 156). The experiment compares three advertising hooks while keeping audience, landing page, budget envelope, creative format, and optimization event constant.

## Scope

- One campaign and one comparable ad set.
- Three deterministic hook variants, each with Dutch, French, and English copy.
- Existing draft-156 landing page and audience remain unchanged.
- Jarvis evaluates the variants in shadow mode first; no Meta mutation is enabled by this setup.
- The existing evidence floors apply per variant: 1,000 impressions, 30 landing-page views, and 10 attributed registrations.
- The existing guardrails remain unchanged: one primary dimension, seven-day maximum test, 24-hour cooldown, one replacement per 24 hours, maximum 20% rolling budget growth, and EUR 20/day/campaign.

## Data model and identity

Each variant receives a stable experiment ID and language-specific publication identity. The frozen decision records the experiment ID, campaign ID, primary dimension `hook`, all variant/publication IDs, landing-page URL, audience/optimization identity, and the exact evidence thresholds used. Replays must produce the same ordering and idempotency key.

## Evaluation

Jarvis only compares variants when the completed Europe/Brussels window is fresh, attribution is available, all variants are comparable, and every candidate has met the evidence floors. Before that point it persists an `observe/not_comparable` decision and does not pause, replace, or scale anything. Ties, missing history, stale data, or contradictory identities also remain neutral.

Once qualified, the existing policy may identify a loser or winner. Replacement is limited to one hook variant per rolling 24 hours; any budget action remains subject to the campaign-level caps and live Meta revalidation.

## CI rollout

All creation, activation, and configuration changes run through GitHub Actions. The first rollout stages the experiment in shadow mode, verifies the three multilingual bundles and exact campaign/landing-page/audience identity, and exposes a read-only inspection report. Meta writes remain disabled until a later explicit canary decision.

## Failure handling and audit

Partial creation, duplicate replay, stale leases, Meta rate limits, publication drift, and attribution failures must leave the existing campaign unchanged or reconcile to a durable blocked state. Slack audit payloads identify the experiment, dimension, variants, thresholds, evidence window, and next evaluation without exposing credentials or raw creative payloads.

## Success criteria

1. A CI-run shadow setup produces exactly three hook variants in NL/FR/EN under the draft-156 campaign.
2. A read-only report proves all variants share the intended landing page, audience, optimization, and budget envelope.
3. An unqualified run produces no Meta mutation and a durable neutral audit.
4. Qualified synthetic evidence selects the same winner/loser independent of input ordering.
5. Duplicate/retry and partial-failure tests prove no duplicate bundle or untracked mutation.
