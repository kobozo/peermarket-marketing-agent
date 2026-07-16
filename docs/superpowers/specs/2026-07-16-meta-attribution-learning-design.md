# Meta Attribution and Learning Design

## Goal

PeerMarket and Jarvis must form a consent-aware measurement loop for paid Meta
campaigns. PeerMarket records first-party campaign attribution and aggregate
funnel events. Jarvis collects Meta delivery metrics, joins them to those
aggregates, reports performance, and produces evidence-backed recommendations.

The loop measures and recommends. It never changes spend, targeting, delivery
state, or published creative without a new explicit founder approval.

## Repository boundary

This feature spans two independently deployed repositories:

- `kobozo/secondhand` owns consent, campaign attribution, funnel-event storage,
  privacy controls, retention, and aggregate read access.
- `kobozo/peermarket-marketing-agent` owns tagged Meta destinations, Meta
  Insights ingestion, delivery monitoring, performance summaries, evidence
  evaluation, and Slack notifications.

Neither deployment may assume that an unmerged change in the other repository
already exists. PeerMarket deploys first. Jarvis attribution ingestion remains
disabled until a production aggregate-read smoke test succeeds.

## Privacy and consent boundary

Attribution is first-party and disabled by default. PeerMarket presents two
clear choices in English, Dutch, and French:

- `Essential only` leaves attribution disabled.
- `Allow analytics` enables campaign attribution and may be withdrawn later.

Consent is stored separately from the existing essential cookies. Before
analytics consent, PeerMarket does not persist UTM parameters, `fbclid`, a
marketing visitor identifier, or marketing funnel events. Consent withdrawal
expires the attribution cookie and prevents new marketing events. Previously
collected data follows the documented retention policy and user privacy
controls.

PeerMarket uses a signed, opaque, random first-party visitor identifier. The
attribution store must not contain an email address, IP address, display name,
raw session token, or Meta access token. An internal user ID may be attached
after registration so downstream events can be deduplicated, but Jarvis may
only read aggregate counts and never user-level rows.

Meta Pixel, Meta Conversions API, Google Analytics, third-party tracking
scripts, cross-site profiling, and retroactive attribution are out of scope.

The cookie banner and privacy policy must stop claiming that PeerMarket does
not advertise or perform analytics. Updated text must describe first-party
campaign measurement, the exact captured fields, purpose, retention, consent
controls, and absence of third-party pixels. Attribution records linked to a
user must participate in data export, withdrawal, and deletion/anonymization
flows.

## Campaign identity and tagged destinations

Every newly generated Meta draft receives a stable campaign identity:

- `utm_source=facebook`
- `utm_medium=paid_social`
- `utm_campaign=peermarket`
- `utm_content=draft-<draft_id>`

The draft ID is the primary join key between Meta publication data and
PeerMarket aggregates. Meta campaign, ad-set, creative, and ad IDs remain the
delivery-side identifiers. Query parameters are added by a single URL builder
that preserves existing query parameters and rejects non-HTTPS PeerMarket
destinations.

Draft 156 has no tagged destination and cannot receive retroactive first-party
attribution. Its Meta delivery metrics remain useful as an explicitly
unattributed baseline.

## PeerMarket attribution model

PeerMarket stores three bounded concepts:

1. A consent record containing the consent choice and timestamps.
2. A campaign touch containing the opaque visitor ID, allowed campaign
   parameters, first/last-touch timestamps, optional internal user ID, and an
   expiry timestamp.
3. Deduplicated funnel events containing event type, campaign identity,
   visitor or internal user identity, subject identity where needed, and event
   timestamp.

Allowed funnel event types are:

- `landing_view`;
- `registration_completed`;
- `first_listing_created`;
- `first_listing_published`;
- `identity_verification_completed`.

`landing_view` is deduplicated per visitor, campaign content, and UTC day.
Registration is recorded once per user. Each `first_*` event is recorded once
per user and only for the user's first qualifying object or transition.
Identity verification is recorded once on the first transition to verified.

Attribution uses first-touch and last-touch values internally. Jarvis initially
queries last-touch aggregates so each conversion is assigned to at most one
campaign. Aggregate responses are grouped by `utm_content`, event type, and UTC
day. They contain counts only and suppress no-data groups rather than exposing
zero-sized user segments.

The default retention period is 90 days and is configurable as a non-secret
GitHub repository variable. A scheduled cleanup removes expired anonymous
touches and events while preserving only non-identifying aggregate reporting
required by the documented policy.

## Aggregate read contract

Jarvis uses the existing read-only PeerMarket database connection. PeerMarket
provides a stable SQL view for aggregate attribution rather than granting
Jarvis access to raw attribution tables. The view exposes:

- UTC day;
- `utm_source`, `utm_medium`, `utm_campaign`, and `utm_content`;
- event type;
- aggregate event count.

The production read-only database role receives `SELECT` on that view only.
Schema availability is detected during the Jarvis job. Missing view,
permission, or configuration disables only the attribution join, records a
sanitized diagnostic, and sends one deduplicated founder alert. It never stops
Meta delivery ingestion.

## Meta Insights ingestion

The existing Jarvis hourly loop calls a dedicated Meta Insights collector for
every Meta publication that has an ad ID and is not terminal. For each
publication, it fetches a rolling three-day window with Meta's account timezone
and stores:

- spend;
- impressions and reach;
- clicks and inline link clicks;
- outbound clicks and landing-page views;
- CTR, CPC, and CPM;
- frequency;
- configured and effective delivery status;
- Meta-reported actions when present;
- window start/stop, retrieval time, and API attribution metadata.

`publications.performance` keeps the latest cumulative snapshot, previous
snapshot, derived non-negative deltas, last successful retrieval time, and
sanitized ingestion error state. The update is idempotent for the same ad,
metric window, and retrieved values. A decrease caused by Meta restatement
replaces the cumulative snapshot but produces a zero delta and records a
restatement marker instead of negative performance.

Failures are isolated per publication. Rate limits and transient Meta errors
receive bounded exponential retry inside the hourly run. Permanent permission
or configuration errors are stored and generate one deduplicated Slack alert.
Raw API responses and credentials are never logged.

## Delivery monitoring

The collector classifies delivery without mutating Meta:

- `healthy`: active and receiving impressions;
- `reviewing`: a documented Meta review state;
- `no_delivery`: configured active for at least two hours with zero
  impressions;
- `rejected_or_error`: effective status or issues indicate rejection/error;
- `terminal`: archived or deleted;
- `unknown`: insufficient or unavailable status data.

Jarvis alerts immediately for `rejected_or_error` and after the two-hour grace
period for `no_delivery`. Alerts are deduplicated by publication, condition,
and observed state. Recovery sends one resolution message. `healthy`,
`reviewing`, and `unknown` do not trigger repeated alerts.

## Attribution join and daily follow-up

Once per day, after both source jobs have had time to finish, Jarvis joins each
tagged publication's Meta snapshot to PeerMarket aggregate events using
`utm_content=draft-<draft_id>` and aligned UTC dates. It calculates only when
the denominator is non-zero:

- cost per link click;
- click-to-landing rate using Meta landing-page views;
- landing-to-registration conversion;
- cost per registration;
- registration-to-first-listing conversion;
- cost per first published listing;
- identity-verification conversion.

The joined evidence snapshot is stored in `publications.performance`. The
daily Slack message includes budget, spend, delivery state, impressions,
clicks, Meta landing-page views, first-party landing views, registrations,
listings, calculated rates/costs, data window, and attribution availability.
Missing data is stated as unavailable and never rendered as a zero conversion.

## Learning rules

Jarvis distinguishes observations from reusable learnings:

- Any completed daily window may create an immutable evidence observation.
- A single ad never creates a general creative or audience conclusion.
- A reusable delivery learning requires at least two comparable variants,
  each with at least 1,000 impressions and 30 Meta landing-page views.
- A reusable conversion learning additionally requires at least 10 attributed
  registrations per compared variant.

Comparable variants share channel, objective, language, audience profile, and
evaluation window definition. Evidence records include publication IDs,
metric window, sample sizes, compared values, and the deterministic threshold
decision. `learnings` is created or reinforced only when all thresholds pass.
Low-data or non-comparable results stay observations.

Draft generation receives only recent learnings matching channel, objective,
language, and audience. Recommendations may propose a new creative, audience,
or destination hypothesis, but every paid draft still follows the existing
founder approval workflow.

## Configuration and deployment

Sensitive values remain GitHub repository secrets. No new secret is required
for first-party attribution because Jarvis uses its existing read-only
PeerMarket database URL and existing Meta credentials.

Non-sensitive GitHub repository variables include:

- attribution enabled flag, default `false`;
- attribution retention days, default `90`;
- Meta Insights enabled flag, default `false` until deployed;
- Insights lookback days, default `3`;
- no-delivery grace hours, default `2`;
- learning minimum impressions, default `1000`;
- learning minimum landing-page views, default `30`;
- learning minimum registrations, default `10`.

Both repositories deploy through reviewed pull requests and their existing
GitHub Actions workflows. PeerMarket CI deploys migrations, consent UI, policy
text, event hooks, cleanup, aggregate view, and read-only grant first. A
production smoke test verifies opt-out, opt-in, event deduplication, and the
aggregate view. Jarvis CI then deploys URL tagging and disabled collectors,
verifies the aggregate view through the read-only connection, enables Insights,
and finally enables attribution.

No production database is edited by hand. Rollback is performed by disabling
the relevant non-sensitive feature flag through CI and reverting the deployed
commit.

## Testing and verification

PeerMarket tests cover:

- essential-only and analytics-consent behavior in all three languages;
- no attribution persistence before consent;
- signed visitor identity and allowed-parameter validation;
- first/last touch and UTC-day landing deduplication;
- anonymous-to-user linking;
- all first-event transition hooks and idempotency;
- withdrawal, export, deletion/anonymization, and retention cleanup;
- aggregate view counts and absence of user-level columns;
- updated privacy and cookie claims.

Jarvis tests cover:

- URL tagging and query preservation;
- Insights pagination, field parsing, time windows, and bounded retry;
- snapshot idempotency, delta derivation, and Meta restatements;
- per-publication error isolation and deduplicated alerts;
- delivery classification and recovery;
- aggregate-view absence and permission failure;
- UTC-window attribution joins and denominator guards;
- observation and reusable-learning thresholds;
- absence of automatic Meta mutations or budget changes.

Production verification uses a consented test visit with a dedicated test
campaign content key. It confirms the landing event appears only in the
aggregate view, contains no personal fields, and is readable by Jarvis. The
test record is then removed through the normal retention/test cleanup path.
Meta verification confirms that draft 156's delivery data is ingested as an
unattributed baseline and that no live Meta object is changed by the new jobs.

## Non-goals

This design does not include Meta Pixel, Conversions API, third-party analytics,
automatic budget changes, automatic pausing, automatic audience expansion,
automatic publication, user-level access from Jarvis, retroactive attribution,
or conclusions drawn from one underpowered advertisement.
