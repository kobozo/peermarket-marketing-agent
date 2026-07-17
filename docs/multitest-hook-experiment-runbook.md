# Draft 156 multilingual hook experiment

This canary is restricted to Draft 156 and Meta campaign `120249125021520342`. It prepares exactly three hook variants, each containing NL, FR, and EN copy, while audience, optimization, format, visual, delivery, ad set, and landing page stay fixed. Preparation stores append-only local records; shadow mode must remain enabled and does not create, update, pause, or activate anything in Meta.

## Configure and dispatch through CI

First generate and review the deterministic experiment ID in a non-production environment. Set only GitHub repository variables; never put credentials or hook copy in variables.

```bash
gh variable set META_INSIGHTS_ENABLED --body true
gh variable set PEERMARKET_ATTRIBUTION_ENABLED --body true
gh variable set META_AUTONOMY_CAMPAIGN_IDS_CSV --body 120249125021520342
gh variable set META_AUTONOMY_EXPERIMENT_ID --body '<frozen-experiment-id>'
gh variable set META_AUTONOMY_VARIANT_COUNT --body 3
gh variable set META_AUTONOMY_ENABLED --body true
gh variable set META_AUTONOMY_SHADOW --body true
gh variable set META_AUTONOMY_MAX_INCREASE_PERCENT --body 20
gh variable set META_AUTONOMY_MAX_DAILY_BUDGET_EUR --body 20
gh variable set META_AUTONOMY_MAX_REPLACEMENTS_24H --body 1
gh variable set META_AUTONOMY_MAX_TEST_DAYS --body 7
gh variable set META_AUTONOMY_COOLDOWN_HOURS --body 24

dispatch_hook_canary() {
  local deploy_ref head_sha boundary attempt run_id run_output
  local -a run_ids
deploy_ref="$(git branch --show-current)"
  head_sha="$(git rev-parse HEAD)"
  boundary="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
gh workflow run deploy.yml --ref "$deploy_ref"
  for attempt in {1..12}; do
    run_output="$(gh run list --workflow deploy.yml --event workflow_dispatch --branch "$deploy_ref" --commit "$head_sha" --created ">=$boundary" --limit 20 --json databaseId --jq ".[].databaseId")" || return 1
    run_ids=()
    while IFS= read -r candidate; do
      [[ -n "$candidate" ]] && run_ids+=("$candidate")
    done <<<"$run_output"
    if (( ${#run_ids[@]} > 1 )); then return 1; fi
    if (( ${#run_ids[@]} == 1 )); then
      run_id="${run_ids[0]}"
      [[ "$run_id" =~ ^[0-9]+$ ]] || return 1
      gh run watch "$run_id" --exit-status
      return
    fi
    sleep 5
  done
  return 1
}
dispatch_hook_canary
```

Do not continue unless the watched workflow succeeded and its commit matches the intended revision.
The deploy workflow automatically runs `peermarket-performance prepare-hook-experiment --draft-id 156 --seed draft-156-shadow-v1` after migrations when `META_AUTONOMY_EXPERIMENT_ID` is configured. This is the runtime path that writes the atomic 3×3 local records; it remains shadow-only and has no Meta/action mutation surface.

## Verify

Run the read-only inspection on the deployed host:

```bash
peermarket-performance autonomy --draft-id 156
```

The sanitized `hook_experiment` result must show the configured experiment ID, `variant_count: 3`, exactly `:01`, `:02`, and `:03` with EN/FR/NL, `fixed_identity_match: true`, `ready: true`, and no blocked reason. Confirm the fixed landing page and audience identity, a sufficient or insufficient evidence state, no queued action, and a durable Slack audit. It intentionally omits hook text, fixed identity values, tokens, and credentials.

Keep shadow true and execution writes disabled until an explicit later canary approval. Record the correlated successful CI run ID and commit SHA in the handoff; this implementation task does not dispatch it.

## Kill switch

If any identity, readiness, CI, reconciliation, or unexpected Meta-state check fails, restore the kill switch and redeploy through CI:

```bash
gh variable set META_AUTONOMY_ENABLED --body false
gh variable set META_AUTONOMY_SHADOW --body true
gh workflow run deploy.yml --ref "$deploy_ref"
```

Leave the experiment ID and records intact for audit. Do not delete or rewrite append-only experiment rows, and do not enable production execution from this runbook.
