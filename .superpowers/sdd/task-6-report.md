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

## Real collector and cycle remediation

Hourly collection now resolves the persisted replacement progress for the configured experiment, reads Insights for all nine ad IDs, joins aggregate registrations by variant/locale UTM identity, and durably merges impressions, LPV, registrations, account window, UTC window, and capture time into the source publication.

The loader validates exact IDs, current campaign, hook dimension, and one fixed campaign/ad-set/landing/fixed-identity control tuple before aggregation. A PostgreSQL collector test proves nine real Insights results are written. A PostgreSQL candidate-cycle test then consumes collected-format metrics through the real loader/snapshot/policy: qualified evidence produces a deterministic non-observe decision, while lowering one variant below floors produces `insufficient_evidence`. Its real policy decision also generates the complete three-member sanitized Slack audit.

Focused GREEN: collector `1 passed`; real candidate cycle/audit `1 passed`; persisted aggregation/audit `2 passed`; experiment policy `4 passed`; deploy/runbook `5 passed`. Ruff and diff checks passed.
Final remediation adds deterministic per-ad `utm_content=<variant>:<locale>` identities on the real Task 5 Meta payload path, preserves existing URL parameters/fragments, binds collected samples to the frozen autonomy evidence window, proves non-zero registration attribution, and exercises neutral then qualified persistence through the real PostgreSQL `run_autonomy_cycle` entry point. Audit assertions include the exact three member IDs, thresholds, window, samples, and next evaluation while excluding raw creative and credential-like fields.
Verifier-boundary remediation centralizes locale destination mapping and reuses it for Meta creative creation, live hook reads, and reverse-cleanup identity fencing. The SDK-boundary test accepts the exact locale UTM creative identity and rejects a wrong destination before identity can be considered verified; deploy tests enumerate every required neutral/shadow audit field.

Whole-branch regression remediation canonicalizes recursively frozen mappings and sequences before snapshot hashing, restoring deterministic replay for ordinary claims instead of cancelling them as `stale_snapshot`. Hook experiment provenance is now passed explicitly only after the persisted nine-row loader validates exact membership; arbitrary ordinary variant IDs cannot opt into hook rules. Immutable evidence also supports non-mutating mapping union and safe deepcopy for persisted replacement adoption. A full PostgreSQL run reached `912 passed`; the final source-race and deploy-interpreter additions are verified separately before the final full rerun.

The hook replacement saga now rereads the source after matrix activation and immediately before source pause, validating frozen hierarchy IDs, budget, and ACTIVE status through `_source_ok`. The race regression changes the source budget during matrix activation and proves the saga reconciles without pausing the source. Final post-review PostgreSQL suite: `916 passed in 163.43s`.
