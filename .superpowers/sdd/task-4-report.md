# Task 4 report — shadow hook experiment integration

## Outcome

- Added `prepare_hook_experiment(...)` for Draft 156 and campaign `120249125021520342`. It requires shadow mode, the campaign allowlist, exactly three variants, and an optional configured-ID match before atomically recording the frozen experiment. It has no Meta call or action-enqueue path.
- Extended the read-only autonomy inspection with a sanitized experiment projection: experiment/variant IDs, languages, variant count, fixed-identity consistency, readiness, and a stable blocked reason. Hook copy and identity values are excluded.
- Wired `META_AUTONOMY_EXPERIMENT_ID` (empty default) and `META_AUTONOMY_VARIANT_COUNT` (`3`) as GitHub Variables in deploy configuration.
- Added the CI-only Draft 156 runbook with shadow verification and kill-switch procedure. No workflow was dispatched, no variables were changed, and nothing was deployed.

## RED evidence

Initial focused run:

`uv run pytest -q tests/test_autonomy_loop.py::test_prepare_hook_experiment_persists_exactly_three_without_meta_mutation tests/test_autonomy_loop.py::test_prepare_hook_experiment_rejects_non_shadow_or_wrong_identity tests/test_cli_performance.py::test_hook_experiment_projection_is_sanitized_and_ready tests/test_deploy_workflow.py::test_deploy_wires_safe_autonomy_variables tests/test_deploy_workflow.py::test_hook_experiment_runbook_is_ci_only_shadow_first_and_has_kill_switch`

Result: `2 failed, 3 passed`. Failures identified the missing hook-experiment runbook and a sanitization-test assertion that collided with the required `fixed_identity_match` key. The assertion was narrowed to an actual sensitive identity value before GREEN.

## GREEN evidence

- `uv run ruff check ...` — all checks passed.
- `uv run pytest -q tests/test_autonomy_loop.py -k 'prepare_hook_experiment'` — `2 passed, 12 deselected`.
- `uv run pytest -q tests/test_cli_performance.py -k 'hook_experiment_projection or autonomy_command'` — `2 passed, 17 deselected`.
- `uv run pytest -q tests/test_deploy_workflow.py` — `5 passed`.
- `git diff --check` — clean.

The broader three-file run produced `28 passed, 1 skipped`; nine database-backed autonomy tests could not set up because `AGENT_DB_URL` is absent in this environment. Those errors occurred before test execution and are unrelated to Task 4 behavior.

## Review rejection remediation

The rejected revision exposed two material gaps, now closed:

- The deployed CI path now runs `peermarket-performance prepare-hook-experiment --draft-id 156 --seed draft-156-shadow-v1` after migrations whenever an experiment ID is configured. The command loads the actual Draft 156 publication IDs, draft metadata, and database brand voice, then calls the shadow-only preparation boundary. The Task 2 store supplies the atomic transaction for all nine variant/language rows.
- Readiness now compares persisted rows with the actual Draft 156 publication campaign/ad-set, draft landing URL/fixed identity, `changed_dimension=hook`, configured experiment ID, exact `:01`/`:02`/`:03` IDs, and exact NL/FR/EN coverage.
- Adversarial tests independently corrupt ad-set, URL, dimension, and variant ID and require a blocked result.
- The preparation test instruments `enqueue_action` and `execute_production_claim` and proves neither boundary is called.

Review RED: the runbook/workflow test failed because no deployed path invoked `prepare-hook-experiment`; the adversarial variant-ID test then failed with `experiment_incomplete` rather than the more precise `variant_ids_mismatch`. Both contracts were corrected before GREEN.

Review GREEN:

- Focused preparation, CLI projection/adversarial, workflow, and runbook tests pass.
- Ruff format/check pass on all Task 4 files.
- Clean database broader run: `42 passed, 2 failed`. Both failures are existing production budget-write expectations in `test_three_publication_scale_preserves_allocation_rounding_and_audits` (`success` and `partial_write_failure`): the lifecycle reports one executed action but their mocked Meta budget write list remains empty. Neither failure enters hook preparation or the new CLI/runtime code.
- No deployment, workflow dispatch, GitHub variable change, action enqueue, or Meta mutation was performed.
