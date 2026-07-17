# Draft 156 multilingual hook experiment

This canary is restricted to Draft 156 and Meta campaign `120249125021520342`. It prepares exactly three hook variants, each containing NL, FR, and EN copy, while audience, optimization, format, visual, delivery, ad set, and landing page stay fixed. Preparation stores append-only local records; shadow mode must remain enabled and does not create, update, pause, or activate anything in Meta.

## Configure and dispatch through CI

First generate and review the deterministic experiment ID in a non-production environment. Set only GitHub repository variables; never put credentials or hook copy in variables.

```bash
gh variable set META_AUTONOMY_CAMPAIGN_IDS_CSV --body 120249125021520342
gh variable set META_AUTONOMY_EXPERIMENT_ID --body '<frozen-experiment-id>'
gh variable set META_AUTONOMY_VARIANT_COUNT --body 3
gh variable set META_AUTONOMY_ENABLED --body true
gh variable set META_AUTONOMY_SHADOW --body true

deploy_ref="$(git branch --show-current)"
gh workflow run deploy.yml --ref "$deploy_ref"
run_id="$(gh run list --workflow deploy.yml --event workflow_dispatch --branch "$deploy_ref" --limit 1 --json databaseId --jq '.[0].databaseId')"
gh run watch "$run_id" --exit-status
```

Do not continue unless the watched workflow succeeded and its commit matches the intended revision.

## Verify

Run the read-only inspection on the deployed host:

```bash
peermarket-performance autonomy --draft-id 156
```

The sanitized `hook_experiment` result must show the configured experiment ID, `variant_count: 3`, three variant IDs each with EN/FR/NL, `fixed_identity_match: true`, `ready: true`, and no blocked reason. It intentionally omits hook text, the fixed identity values, tokens, and credentials. Confirm separately in Meta that no resources changed during shadow preparation.

## Kill switch

If any identity, readiness, CI, reconciliation, or unexpected Meta-state check fails, restore the kill switch and redeploy through CI:

```bash
gh variable set META_AUTONOMY_ENABLED --body false
gh variable set META_AUTONOMY_SHADOW --body true
gh workflow run deploy.yml --ref "$deploy_ref"
```

Leave the experiment ID and records intact for audit. Do not delete or rewrite append-only experiment rows, and do not enable production execution from this runbook.
