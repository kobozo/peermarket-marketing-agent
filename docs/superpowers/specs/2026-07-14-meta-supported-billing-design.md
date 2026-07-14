# Meta Supported Billing Design

## Evidence and problem

Draft 156 reaches campaign creation but Meta rejects ad-set creation with error
code `100`, subcode `2446404`, title `Billing Option Not Available`. The request
uses `billing_event=LINK_CLICKS`. Meta explicitly reports that this ad account is
not yet eligible for that billing option.

Two incomplete paused campaigns exist from the failed attempts:
`120249090306330342` and `120249094999250342`. Neither attempt created an ad
set, creative, or ad.

## Considered approaches

1. **Use impression billing now (selected).** Send
   `billing_event=IMPRESSIONS` while retaining
   `optimization_goal=LINK_CLICKS`. This uses the broadly available billing
   event while still optimizing delivery for link clicks.
2. Retry link-click billing and fall back after subcode `2446404`. This creates
   an avoidable incomplete campaign on every first attempt and is therefore
   rejected.
3. Make the billing event an environment setting. This adds operational surface
   without a current need and could reintroduce an unsupported value, so it is
   rejected for now.

## Implementation

Change only the ad-set request in `meta_ads.py` from `LINK_CLICKS` to
`IMPRESSIONS`. Add a regression test that inspects the SDK request and requires
the exact supported combination:

```text
billing_event=IMPRESSIONS
optimization_goal=LINK_CLICKS
```

Keep campaign, targeting, budget, creative, Page identity, and paused status
unchanged. Continue preserving Meta error codes and user-facing diagnostics.

## Delivery and proof

Develop test-first in an isolated branch, run focused tests plus Ruff locally,
open a PR, require the complete Postgres-backed GitHub CI suite to pass, merge,
and require the deployment workflow's service and health checks to pass.

After deployment, invoke `process_approved_meta_draft` exactly once for draft
156 on `agent-jarvis`. Completion requires runtime evidence of all of the
following:

- ad-set creation succeeds;
- creative creation succeeds with Page ID `61592144690879`;
- ad creation succeeds;
- the returned ad status is `PAUSED`;
- an Ads Manager URL and concrete campaign, ad-set, creative, and ad IDs exist.

If Meta rejects a later stage, preserve the exact diagnostic, fix the newly
proven incompatibility through the same CI path, and retry until those success
conditions are met. Do not activate spend automatically.
