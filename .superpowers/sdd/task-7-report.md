# Task 7 report

Implemented the CI-only Draft 156 hook canary contract without changing repository variables, dispatching workflows, deploying, or mutating Meta.

- Stages insights, attribution, autonomy shadow, the single campaign allowlist, three-variant experiment identity, and all budget/cooldown limits through documented `gh variable set` commands.
- Uses exact branch, commit SHA, UTC boundary, and a unique numeric workflow run ID before `gh run watch --exit-status`.
- Adds a guarded deployed read-only `peermarket-performance autonomy --draft-id 156` gate that verifies enabled shadow flags, the allowlist, exact ordered `:01/:02/:03` membership, fixed identity readiness, and no action.
- Documents the evidence state and durable Slack audit checks, the handoff requirement for the successful correlated CI run, and the explicit later approval required before writes.

Verification: `uv run pytest -q tests/test_deploy_workflow.py` (6 passed), YAML loaded by the test suite, and `git diff --check` clean.
