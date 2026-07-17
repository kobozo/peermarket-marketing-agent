# Autonomous Ad Lifecycle Rollout Runbook

This runbook prepares the Draft 156 canary for Meta campaign
`120249125021520342`. All configuration changes go through GitHub Actions
repository variables and `deploy.yml`. Do not edit the host environment by hand,
put credentials in variables, or paste credentials into commands, logs, or tickets.

## Safety envelope

The canary ceiling is EUR 20 per day, with at most a 20 percent increase, one
replacement in 24 hours, a seven-day maximum test, and a 24-hour mutation
cooldown. Draft 156 is the only initial inspection target and its campaign is the
only allowlisted campaign.

From an authenticated maintainer workstation in the repository, stage the safe
disabled configuration:

```bash
gh variable set META_AUTONOMY_ENABLED --body false
gh variable set META_AUTONOMY_SHADOW --body true
gh variable set META_AUTONOMY_CAMPAIGN_IDS_CSV --body 120249125021520342
gh variable set META_AUTONOMY_MAX_INCREASE_PERCENT --body 20
gh variable set META_AUTONOMY_MAX_DAILY_BUDGET_EUR --body 20
gh variable set META_AUTONOMY_MAX_REPLACEMENTS_24H --body 1
gh variable set META_AUTONOMY_MAX_TEST_DAYS --body 7
gh variable set META_AUTONOMY_COOLDOWN_HOURS --body 24
```

Confirm the values with `gh variable list`. They are operational configuration,
not credentials. Do not run secret-management commands as part of this rollout.

## Deploy the disabled configuration

Dispatch only the tested CI workflow and watch it finish:

```bash
gh workflow run deploy.yml
gh run watch
```

On the self-hosted runner, the workflow runs migrations before restarting the
services and verifies systemd plus the health endpoint. An operator may repeat
the read-only health check on the host:

```bash
curl -fsS http://127.0.0.1:8090/agent/healthz
```

Require an HTTP success and `{"status":"ok"}`. Stop if either service is not
active or health is not OK.

## Enter shadow mode

The master flag is deliberately false in the first deployment, so it cannot
evaluate, enqueue, or mutate. After health is confirmed, enable evaluation while
keeping shadow mode true, then deploy through CI again:

```bash
gh variable set META_AUTONOMY_ENABLED --body true
gh variable set META_AUTONOMY_SHADOW --body true
gh workflow run deploy.yml
gh run watch
```

Wait for a successful hourly collection. On the host, inspect only the persisted,
sanitized state through the PostgreSQL read-only CLI transaction:

```bash
peermarket-performance autonomy --draft-id 156
```

The report must show Draft 156 exists, campaign `120249125021520342` is
allowlisted, shadow is true, a recent decision and evidence window are present,
and no action was queued. It must not show raw PeerMarket records or credentials.
Repeat the health check after inspection.

## Enable the canary

Enable writes only after the shadow record is current, internally consistent,
and free of a reconciliation block. Keep the master flag true and change only
shadow mode:

```bash
gh variable set META_AUTONOMY_ENABLED --body true
gh variable set META_AUTONOMY_SHADOW --body false
gh workflow run deploy.yml
gh run watch
```

After deployment, check health and run the same Draft 156 read-only inspection.
Confirm budgets remain within EUR 20, the latest action has a verified audit
result, and Slack has the newest campaign lifecycle notice.

## Kill switch and reconciliation

`META_AUTONOMY_ENABLED=false` is the immediate kill switch. Use it for unexpected
Meta state, an unverified rollback, stale evidence, service instability, or any
`reconciliation_required` action:

```bash
gh variable set META_AUTONOMY_ENABLED --body false
gh variable set META_AUTONOMY_SHADOW --body true
gh workflow run deploy.yml
gh run watch
```

Verify health, then inspect Draft 156 again with the read-only command. Do not
manually retry, clear, or overwrite a reconciliation record. Compare the frozen
publication identities, live Meta hierarchy, budgets, rollback audit, and latest
campaign notice. Resolve the external state with founder approval and leave the
master flag false until reconciliation is proven and recorded. Re-enter shadow
mode before considering another live enablement.
