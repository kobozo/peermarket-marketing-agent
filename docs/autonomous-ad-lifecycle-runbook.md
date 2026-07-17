# Autonomous Ad Lifecycle Rollout Runbook

This runbook prepares the Draft 156 canary for Meta campaign
`120249125021520342`. All configuration changes go through GitHub Actions
repository variables and `deploy.yml`. Do not edit the host environment by hand,
put credentials in variables, or paste credentials into commands, logs, or tickets.

Define this fail-closed dispatch helper once in the maintainer shell. It captures
the exact branch, commit, and UTC boundary before dispatch. Because workflow-run
creation is asynchronous, it polls for at most one minute. Zero matches keep
polling; multiple matches, a nonnumeric ID, timeout, or a failed run stop rollout.

```bash
dispatch_deploy() {
  local deploy_ref head_sha boundary attempt run_id run_output
  local -a run_ids
  deploy_ref="$(git branch --show-current)"
  head_sha="$(git rev-parse HEAD)"
  boundary="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  if [[ -z "$deploy_ref" || ! "$head_sha" =~ ^[0-9a-f]{40}$ ]]; then
    echo "deploy requires a named branch and exact local commit SHA" >&2
    return 1
  fi
  gh workflow run deploy.yml --ref "$deploy_ref" || return 1
  for attempt in {1..12}; do
    run_output="$(gh run list --workflow deploy.yml --event workflow_dispatch --branch "$deploy_ref" --commit "$head_sha" --created ">=$boundary" --limit 20 --json databaseId --jq ".[].databaseId")" || return 1
    run_ids=()
    if [[ -n "$run_output" ]]; then
      mapfile -t run_ids <<<"$run_output"
    fi
    if (( ${#run_ids[@]} > 1 )); then
      echo "ambiguous deploy runs for exact ref/SHA/boundary" >&2
      return 1
    fi
    if (( ${#run_ids[@]} == 1 )); then
      run_id="${run_ids[0]}"
      if [[ ! "$run_id" =~ ^[0-9]+$ ]]; then
        echo "deploy run databaseId is not numeric" >&2
        return 1
      fi
      gh run watch "$run_id" --exit-status
      return
    fi
    sleep 5
  done
  echo "deploy run did not appear within 60 seconds" >&2
  return 1
}
```

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
gh variable set META_INSIGHTS_ENABLED --body true
gh variable set PEERMARKET_ATTRIBUTION_ENABLED --body true
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
dispatch_deploy
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
dispatch_deploy
```

Wait for a successful hourly collection. On the host, inspect only the persisted,
sanitized state through the PostgreSQL read-only CLI transaction:

```bash
peermarket-performance autonomy --draft-id 156
```

The report must show Draft 156 exists, campaign `120249125021520342` is
allowlisted, shadow is true, a recent decision and evidence window are present,
and no action was queued. Confirm collection is live from the recent evidence
window and `attribution_complete`; otherwise autonomy is inert and must not be
enabled. It must not show raw PeerMarket records or credentials.
For the hook experiment, require one campaign-scoped audit containing the configured
experiment ID, all three ordered variant IDs, thresholds, per-variant samples, the
complete fresh evidence window, and next evaluation. `insufficient_evidence`,
`neutral_tie`, `stale_snapshot`, and `missing_attribution` are neutral and must not
trigger Meta mutation.
Repeat the health check after inspection.

## Enable the canary

Enable writes only after the shadow record is current, internally consistent,
and free of a reconciliation block. Keep the master flag true and change only
shadow mode:

```bash
gh variable set META_AUTONOMY_ENABLED --body true
gh variable set META_AUTONOMY_SHADOW --body false
dispatch_deploy
```

After deployment, check health and run the same Draft 156 read-only inspection.
Confirm budgets remain within EUR 20, `reconciliation_blocked` is false, and the
latest action's sanitized `status`, `failure_category`, and `rollback_recorded`
fields agree with the expected outcome. Slack must have the newest campaign
lifecycle notice. The CLI intentionally does not claim external audit verification.

## Kill switch and reconciliation

`META_AUTONOMY_ENABLED=false` is the immediate kill switch. Use it for unexpected
Meta state, an unverified rollback, stale evidence, service instability, or any
`reconciliation_required` action:

```bash
gh variable set META_AUTONOMY_ENABLED --body false
gh variable set META_AUTONOMY_SHADOW --body true
dispatch_deploy
```

Verify health, then inspect Draft 156 again with the read-only command. Do not
manually retry, clear, or overwrite a reconciliation record. Compare the frozen
publication identities, live Meta hierarchy, budgets, rollback audit, and latest
campaign notice. Resolve the external state with founder approval and leave the
master flag false until reconciliation is proven and recorded. Re-enter shadow
mode before considering another live enablement.
