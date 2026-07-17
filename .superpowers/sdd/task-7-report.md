# Task 7 report

Implemented the CI-only Draft 156 hook canary contract without changing repository variables, dispatching workflows, deploying, or mutating Meta.

- Stages insights, attribution, autonomy shadow, the single campaign allowlist, three-variant experiment identity, and all budget/cooldown limits through documented `gh variable set` commands.
- Uses exact branch, commit SHA, UTC boundary, and a unique numeric workflow run ID before `gh run watch --exit-status`.
- Adds a guarded deployed read-only `peermarket-performance autonomy --draft-id 156` gate that verifies enabled shadow flags, the allowlist, exact ordered `:01/:02/:03` membership, fixed identity readiness, and no action.
- Documents the evidence state and durable Slack audit checks, the handoff requirement for the successful correlated CI run, and the explicit later approval required before writes.

Rejection remediation makes blank `gh run list` polls true zero-match retries (covered by an executable fake-`gh` shell test), extends the PostgreSQL READ ONLY autonomy projection with whitelisted experiment samples/window/qualification, campaign-wide nonterminal action state, and the latest durable Slack audit ID/status, and makes the deploy gate independently compare the staged experiment ID before enforcing exact membership, evidence, zero active actions, and pending/delivered audit state.

Final remediation aligns the gate with every recognized neutral and qualified hook-policy reason, rejects unknown reasons, projects only frozen `policy_limits`, and links the durable audit to the current decision through its decision ID/idempotency key plus matching experiment, ordered variants, and evidence window. Older campaign audits cannot satisfy the SQL lookup or workflow predicate.

The policy-reason classifier now enumerates every OBSERVE reason emitted by `policy.py`, including validation/history/window, delivery diagnosis, cooldown, incomplete experiment, replacement/reallocation limits, and all scale headroom/budget/allocation guards. The non-emitted `technical_delivery_failure` label was removed; both real dynamic delivery diagnoses are covered explicitly.

Verification: `uv run pytest -q tests/test_deploy_workflow.py` (6 passed), YAML loaded by the test suite, and `git diff --check` clean.
