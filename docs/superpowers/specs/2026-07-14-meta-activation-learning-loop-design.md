# Meta Activation and Learning Loop Design

## Goal

An approved Meta draft must complete the entire publishing workflow: create the
campaign resources safely, activate them, verify their effective state, persist
the publication, and report an actionable result. The agent must then collect
Meta delivery metrics and PeerMarket funnel conversions so that future drafts
can be improved using attributable evidence.

## Scope and rollout order

This work is delivered in three independently verifiable stages:

1. Complete and repair Meta activation, including existing draft 156.
2. Collect Meta Insights and produce performance follow-ups.
3. Add first-party attribution to PeerMarket and use joined funnel results as
   evidence for learnings.

Stage 1 is deployed and verified before stages 2 and 3. An attribution failure
must never prevent the activation fix from shipping.

## Approval and spend boundary

The founder's Slack approval is the final authorization to spend the draft's
suggested daily budget. No additional manual Ads Manager activation is required.
The approved budget remains the only budget applied by the pipeline; neither the
learning loop nor the agent may increase it automatically.

## Transactional Meta publication

The connector initially creates campaign, ad set, creative, and ad in `PAUSED`
state. This prevents a partially constructed hierarchy from spending money.
After every creation succeeds, the connector activates the hierarchy in this
order:

1. campaign;
2. ad set;
3. ad.

It then reads the configured and effective status of all three resources. A
publication succeeds only when every configured status is `ACTIVE` and no
effective status represents an error, disapproval, or disabled ancestor. Meta's
normal review state, such as `IN_PROCESS` or `PENDING_REVIEW`, is accepted as a
successfully submitted active ad because delivery cannot begin until Meta
finishes its own review.

If creation or activation fails, the connector makes a best-effort rollback by
setting the ad, ad set, and campaign to `PAUSED` in child-to-parent order. It
returns a structured error containing the failed phase, resource IDs already
created, observed statuses, and any rollback failures. It must not create a
second hierarchy as an automatic retry.

The operation is idempotent at pipeline level. Once a publication record exists
for a draft, retrying that draft reconciles and activates the stored Meta IDs
instead of creating duplicates.

## Publication state and messages

On successful submission:

- insert or update one `publications` row for the draft;
- persist campaign, ad-set, creative, and ad IDs in structured JSON;
- record configured and effective statuses plus the approved daily budget;
- change the draft from `approved` to `published` in the same database
  transaction;
- send Slack the Ads Manager URL, budget, and review/delivery state.

On failure, the draft remains `approved`, the known external IDs and failure
details are retained for reconciliation, and Slack states whether rollback was
complete. The existing inaccurate `Trust score updated` wording is removed
until trust-score calculation is genuinely implemented.

Draft 156 is retried through this same reconciliation path. Its already-created
IDs are used; it must not create another campaign, ad set, creative, or ad.

## Meta Insights ingestion

An hourly job retrieves insights for every non-final Meta publication over a
rolling recent window. It stores cumulative values with the Meta attribution
window and retrieval timestamp, then derives deltas without double-counting.
The minimum metric set is:

- spend;
- impressions and reach;
- link clicks;
- CTR, CPC, and CPM;
- Meta-reported actions when available.

API errors are isolated per publication. Rate limiting and transient errors use
bounded retry with backoff; permanent permission or configuration errors produce
one deduplicated founder alert. Raw API responses are not logged when they may
contain tokens or user-level data.

The hourly job updates `publications.performance`. A daily follow-up summarizes
delivery and funnel movement in Slack. The agent never pauses, scales, or changes
budget from these measurements without a new explicit founder approval.

## PeerMarket first-party attribution

The PeerMarket application captures these landing parameters when present:

- `utm_source`;
- `utm_medium`;
- `utm_campaign`;
- `utm_content`;
- `fbclid`.

It assigns an opaque first-party visitor identifier and records first-touch and
last-touch attribution. When a visitor authenticates or registers, attribution
is linked to the internal user ID. No email address, IP address, name, or raw
session token is copied into the marketing-agent database.

The first funnel events are:

- landing view;
- registration completed;
- first listing created;
- first listing published;
- identity verification completed.

PeerMarket owns these records. The marketing agent keeps read-only access and
queries only aggregate counts grouped by campaign/content and time bucket. For
draft 156, `utm_content=draft-156` is the stable join key. Meta IDs remain the
stable join keys for delivery data.

The privacy policy must be corrected before attribution is enabled: the current
claim that PeerMarket does not run advertisements is no longer true. Tracking is
first-party and limited to campaign attribution in this stage. Meta Pixel and
Conversions API are explicitly out of scope until consent and privacy treatment
are approved separately.

## Learning and follow-up loop

The daily learning job joins Meta delivery metrics with aggregate PeerMarket
events by draft and attribution window. It calculates, when denominators exist:

- cost per link click;
- landing-to-registration conversion;
- cost per registration;
- registration-to-first-listing conversion;
- cost per published listing.

It stores evidence snapshots in `publications.performance`, updates the related
creative's `performance_summary`, and creates or reinforces a `learnings` record
only after a minimum evidence threshold is reached. A learning contains its
metric window, sample size, compared variants or baseline, confidence, and
publication IDs. Lack of data is reported as lack of data, not converted into a
creative conclusion.

Draft generation receives a compact set of recent, relevant learnings for the
same channel, audience, language, and campaign objective. The founder still
approves every paid draft. Trust scores are calculated from actual approval and
publication outcomes in a separate deterministic function; Slack only claims a
trust-score update when that calculation has committed successfully.

## Configuration and CI deployment

All deployments use the existing GitHub Actions deployment workflows. Secrets
remain GitHub repository secrets:

- Meta app secret;
- Meta system-user token;
- database connection strings.

Non-sensitive controls are GitHub repository variables and are written to the
deployed environment by CI:

- automatic activation enabled;
- insights interval and lookback window;
- attribution retention period;
- minimum evidence thresholds.

Safe defaults keep new jobs disabled unless their required configuration is
present. CI must run unit tests before deployment and the deploy smoke test must
verify service health plus database migrations. Tokens and secret values are
never printed.

## Testing and verification

Connector tests cover creation in `PAUSED`, ordered activation, status
verification, acceptable Meta review states, rollback, rollback failure
reporting, and reconciliation without duplication. Pipeline/database tests cover
atomic publication persistence and failure retention. Insights tests cover
pagination, rate limits, cumulative-to-delta handling, and idempotent upserts.

PeerMarket tests cover UTM capture, first/last touch, anonymous-to-user linking,
event deduplication, retention, and aggregate-only marketing queries. Learning
tests use fixed metric fixtures and verify thresholds, attribution windows, and
the absence of conclusions for insufficient samples.

Production verification for draft 156 must show:

1. the stored IDs equal the existing Meta hierarchy;
2. campaign, ad set, and ad have configured status `ACTIVE`;
3. the ad is either delivering or in a valid Meta review state;
4. the publication record exists and draft 156 is `published`;
5. Slack reports the observed state and Ads Manager URL.

## Non-goals

This design does not authorize automatic budget increases, autonomous campaign
pausing, audience expansion, Meta Pixel, Conversions API, user-level profiling,
or retroactive attribution of visitors who arrived without campaign parameters.
