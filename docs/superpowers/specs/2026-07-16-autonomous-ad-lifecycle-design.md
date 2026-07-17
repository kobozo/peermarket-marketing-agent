# Autonomous Meta Ad Lifecycle Design

## Goal

Jarvis autonomously manages the complete Meta ad lifecycle based on measured
delivery and conversion evidence. It may pause weak ads, create and activate
improved replacements, reallocate budget, and increase the campaign's total
daily budget by at most 20% in any rolling 24-hour period.

The system must learn from controlled comparisons instead of reacting to noisy
partial data. Every decision and Meta mutation is durable, explainable,
idempotent, reversible where possible, and reported to Slack.

## Scope

This change adds autonomous execution on top of the existing Meta Insights,
PeerMarket attribution, learning, creative generation, publication, activation,
and pause primitives. It does not introduce new advertising channels or expose
user-level PeerMarket data to Jarvis.

Draft 156 is the first production-controlled candidate. Because it predates the
UTM scheme, it remains a delivery baseline and cannot supply reliable
first-party conversion attribution.

## Architecture

### Lifecycle controller

An isolated lifecycle controller evaluates each eligible active Meta campaign
after a complete measurement window. Its deterministic result is one of:

- `observe`: retain the current configuration and schedule another evaluation;
- `pause`: stop a proven weak ad;
- `replace`: pause a weak ad and create and activate a controlled replacement;
- `reallocate`: move budget between ads or ad sets without increasing the total;
- `scale`: increase the campaign's total daily budget within the hard limit.

The decision object contains the metric window, evidence values, thresholds,
reason, affected Meta resources, old and proposed budgets, creative dimension
under test, next evaluation time, and an idempotency key. The controller does
not call Meta directly.

### Durable action queue

Approved autonomous decisions enter a durable action queue. A worker claims one
action per campaign, re-reads current database and Meta state, validates every
invariant, performs the mutation, and records the result. Claims have leases so
a crashed worker can safely retry. Idempotency keys prevent duplicate actions.

Action states are `pending`, `claimed`, `executing`, `succeeded`, `failed`, and
`rolled_back`. Attempts, sanitized Meta errors, resource identifiers, before and
after values, rollback results, and Slack delivery state are retained.

No campaign may have more than one nonterminal autonomous action. Meta state is
revalidated immediately before every mutation so a founder's or Meta's external
change wins over a stale Jarvis decision.

### Replacement workflow

For a replacement, Jarvis:

1. freezes the evidence and the single creative dimension to change;
2. generates naturally written Dutch, French, and English variants;
3. validates brand, landing URL, UTM values, and Meta prerequisites;
4. creates all replacement Meta resources paused;
5. verifies the created resources and their budgets;
6. activates the replacement;
7. verifies delivery state;
8. pauses the losing ad;
9. records the experiment relationship and next evaluation time.

The previous creative and evidence remain immutable. A replacement changes one
primary dimension per experiment (hook, copy, visual, or audience) so the result
can produce a meaningful learning.

If creation or verification fails, all newly created resources remain paused or
are paused during rollback. The previously working ad remains active. If the new
ad activates but pausing the loser fails, Jarvis immediately pauses the new ad
to prevent unintended parallel spend and reports the incident.

## Decision policy

### Data eligibility

Jarvis uses only completed account-timezone windows. Missing, stale,
contradictory, or partial data yields `observe` and an operational alert. A
single publication cannot generate a comparative reusable learning.

The initial evidence floors reuse the current configurable defaults:

- delivery decision: at least 1,000 impressions and 30 landing-page views per
  comparable variant;
- registration decision: at least 10 attributed registrations per comparable
  variant;
- no-delivery investigation: configured active for at least two hours with zero
  impressions.

If an ad does not reach the evidence floor, a configurable maximum test duration
may end the test, but insufficient evidence never becomes a directional
learning. The initial maximum test duration is seven completed days.

### Delivery and technical failures

`no_delivery`, rejected, or error states first enter diagnosis. Jarvis may retry
safe reads and correct a known recoverable delivery-state mismatch. It does not
generate new creative to hide a technical configuration or permission failure.
Unrecoverable technical failures are reported with the campaign left in its
last verified safe state.

### Weak ads and replacements

An ad is paused or replaced only after a comparable variant clears the evidence
floor and beats it on the declared primary metric, or after the maximum test
duration produces enough valid evidence under a separately configured terminal
rule. Ties remain neutral. All comparisons use exact decimal arithmetic and
persist the decision inputs.

The initial production policy permits at most one replacement per campaign in a
rolling 24-hour period and imposes a 24-hour evaluation cooldown after a
creative, audience, delivery, or budget mutation.

### Reallocation and scaling

Jarvis may reallocate budget from a proven loser to a proven winner without
raising the campaign total. It may increase total daily budget only when the
winner satisfies the evidence policy and has no delivery or attribution health
issue.

Hard invariants:

- cumulative autonomous increases may not exceed 20% of the campaign's daily
  budget at the start of any rolling 24-hour window;
- at most one autonomous total-budget increase per campaign per rolling 24
  hours;
- decreases do not create headroom for another increase within that window;
- currency and Meta minimum/maximum budget constraints are checked before the
  write;
- no action may exceed the initial absolute daily budget ceiling of EUR 20 per
  campaign;
- missing budget history or an indeterminate baseline blocks scaling.

The absolute ceiling is mandatory configuration. Autonomous execution remains
disabled if it is absent or invalid.

## Learning loop

Every completed experiment produces an observation containing variants,
dimensions, exact metrics, sample sizes, window, mutations, and outcome. A
reusable learning is created or reinforced only when the existing delivery or
conversion thresholds pass. Neutral, malformed, nonfinite, stale, or
unattributed outcomes never influence future generation.

New variants consume at most five recent learnings matching channel, objective,
language, audience, and creative dimension. Dutch, French, and English copy is
generated idiomatically per language rather than translated literally.

## Configuration and rollout controls

All production controls are GitHub repository variables or secrets and are
written by the existing CI deployment:

- master autonomous-execution flag, default `false`;
- shadow-mode flag, default `true`;
- allowed campaign IDs, initially only draft 156's campaign;
- maximum replacements per campaign per 24 hours, default `1`;
- mutation cooldown hours, default `24`;
- maximum test duration days, default `7`;
- maximum total budget increase per 24 hours, fixed initially at `20%`;
- absolute campaign daily budget ceiling, initially `20` EUR and required;
- existing evidence and snapshot thresholds.

Flags and limits are snapshotted into every decision. Tightening a limit applies
at execution time even if the queued decision used an older configuration.

Deployment is CI-only and staged:

1. deploy code with execution disabled and shadow mode enabled;
2. run draft 156 through shadow evaluation and inspect its frozen decision;
3. verify evidence, budget math, Meta identifiers, audit data, and Slack output;
4. enable execution for the allowlisted campaign through a GitHub variable;
5. permit one action and verify database, Meta state, Slack, and next evaluation;
6. keep the global kill switch available through CI configuration.

## Slack reporting

Every shadow decision, executed action, failure, rollback, and recovery produces
a durable Slack message. It states what Jarvis observed, thresholds used, what
changed, previous and new budgets, affected ads, whether rollback was needed,
and when Jarvis will evaluate again. These are audit notifications, not approval
requests.

## Failure handling

- Read or data-quality failure: do not mutate; retry and alert.
- Stale decision or external state change: cancel safely and re-evaluate.
- Meta rate limit or transient failure: retry with bounded backoff under the
  same idempotency key.
- Partial resource creation: pause or remove only newly created resources and
  retain the old working ad.
- Activation/pause split failure: restore the last verified single-ad spend
  state; if that cannot be proven, pause newly created resources and alert.
- Database persistence failure before a Meta write: do not call Meta.
- Database persistence failure after a Meta write: reconcile Meta state before
  any new action and block the campaign meanwhile.
- Budget invariant failure: reject the action without a Meta call.

## Testing

Tests are written before implementation and cover:

- every decision outcome and exact evidence boundary;
- incomplete, stale, contradictory, tied, and unattributed data;
- 20% rolling-window budget calculations, decreases, concurrent actions, and
  the mandatory absolute ceiling;
- deterministic idempotency and action leases;
- external Meta changes between decision and execution;
- creation, activation, pause ordering, and every partial-failure rollback;
- duplicate hourly/daily jobs and process crashes;
- language-specific generation inputs and one-dimension experiments;
- learning eligibility and exclusion of bad observations;
- shadow mode proving that no Meta mutation method is reachable;
- feature flags, allowlist, kill switch, and CI variable wiring;
- sanitized audit and Slack payloads without tokens or user-level data.

Production verification is read-only until the explicitly staged one-action
canary. Completion requires green CI, healthy services, a valid shadow decision,
and a reconciled canary action whose database and live Meta states agree.

## Success criteria

- Jarvis can autonomously stop a proven weak ad and activate a controlled
  replacement without founder approval.
- Jarvis can reallocate spend and scale a proven winner while never exceeding
  the 20% rolling 24-hour increase or the absolute ceiling.
- Every mutation is attributable to frozen evidence and has an idempotent audit
  record and Slack notification.
- Partial failures cannot silently leave unintended duplicate spend active.
- Subsequent creatives consume only valid, comparable learnings.
- Draft 156 can pass shadow evaluation before any autonomous production write.
