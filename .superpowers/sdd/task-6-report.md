# Task 6 report — hook experiment evaluation and audit

## Outcome

- Added an explicit experiment-level policy contract for exactly three ordered `:01`/`:02`/`:03` hook variants.
- Decisions remain OBSERVE until all three meet every impression, landing-page-view, and registration floor.
- Winner/loser selection is independent of input ordering; ties, stale snapshots, missing attribution, incomplete windows, and incomplete experiment membership remain neutral.
- Decision evidence now freezes the experiment ID and complete evidence window.
- Slack audit payloads now include sanitized campaign-scoped experiment ID, deterministic variant IDs, thresholds, samples, evidence window, and next evaluation timestamp.
- Updated the autonomous lifecycle runbook with experiment-level rollout verification and neutral outcomes.

## TDD evidence

RED exposed the missing exact-three experiment contract and missing experiment/window audit fields. The first audit GREEN attempt also exposed immutable evidence serialization, and the full policy regression exposed that intentionally untrusted snapshots omit `captured_at`; both paths were corrected without weakening fail-closed behavior.

GREEN commands:

- `uv run pytest -q tests/test_autonomy_policy.py` — `57 passed`.
- `AGENT_DB_URL=postgresql+asyncpg://postgres:test@localhost:55432/agent_test uv run pytest -q tests/test_autonomy_loop.py -k autonomy_audit_freezes_meaningful_sanitized_campaign_content` — `1 passed, 13 deselected`.
- `uv run pytest -q tests/test_deploy_workflow.py` — `5 passed`.
- Ruff format/check passed for all Task 6 source and test files.
- `git diff --check` passed.

No deployment, workflow dispatch, GitHub variable change, Slack delivery, or Meta mutation was performed.

## Production snapshot remediation

The autonomy cycle now reads the configured experiment's nine append-only locale rows, validates exact `:01`/`:02`/`:03` × NL/FR/EN identity, and aggregates persisted per-locale performance into three logical policy variants. The snapshot builder attaches `experiment_id` only for that exact membership, preserving ordinary campaign behavior. A real PostgreSQL test proves nine persisted identities become three deterministic hook samples.

Slack text now explicitly includes experiment ID and the frozen start/end/captured-at evidence window, in addition to its structured fields. The lifecycle runbook and deploy contract test require prepare/inspect plus all-three-ID audit verification.

Remediation GREEN: PostgreSQL snapshot/audit `2 passed, 13 deselected`; experiment policy `4 passed, 53 deselected`; deploy/runbook `5 passed`; Ruff and diff checks passed.
