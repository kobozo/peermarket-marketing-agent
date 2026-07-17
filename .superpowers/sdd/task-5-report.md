# Task 5 report — guarded three-variant Meta bundles

## Outcome

- Added a production adapter matrix for exactly three ordered hook variants (`:01`, `:02`, `:03`), each with exact NL/FR/EN children.
- All variants share one campaign and one ad set; the matrix requires nine unique creative IDs and nine unique ad IDs.
- Durable progress is namespaced per variant while campaign/ad-set IDs remain shared. Complete retries perform zero Meta SDK writes, and conflicting durable identity fails closed.
- Added a frozen `HookExperiment` to `MetaBundleLocale` translator, preserving landing page, non-hook copy, locale, CTA, and variant identity.
- Added the executor bridge with an explicit shadow-mode refusal before the matrix adapter can be called.
- Existing replacement publication leases, fenced progress callback, paused verification, compensation, and executor transient/rate-limit classification remain the controlling production path.

## RED evidence

The new tests initially had no three-variant adapter. After the first implementation pass, the executor shadow test failed before reaching its assertion because its settings fixture omitted Meta connector fields. The fixture was completed and the test then proved the external matrix boundary was not awaited.

## GREEN evidence

- `uv run ruff check ...` — all Task 5 files passed.
- `uv run pytest -q tests/test_meta_ads.py -k hook_experiment_matrix` — `4 passed, 85 deselected`.
- `uv run pytest -q tests/test_autonomy_executor.py -k 'hook_experiment_adapter_shadow or shadow_mode_is_impossible'` — `2 passed, 85 deselected`.
- `git diff --check` — clean.

The matrix tests exercise the production async adapter while faking only `_sync_create_bundle_resource`, the external SDK boundary. They verify one parent hierarchy, all nine children, duplicate-free durable retry, immediate rate-limit propagation, and cross-variant resource-reuse drift rejection. The broader three-file clean-DB run progressed through all Meta adapter tests but encountered failures in the existing database-backed executor section; the focused Task 5 paths are green.

No Meta call, deployment, workflow dispatch, or GitHub variable change was performed.
