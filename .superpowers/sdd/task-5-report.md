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

## Saga-integration remediation

Following review rejection, the production replacement path now recognizes the configured frozen experiment ID, reconstructs the exact `HookExperiment` from all nine append-only database rows, and dispatches the matrix through `_replace` rather than exposing only a low-level adapter.

The production adapter first enters the established `publish_replacement_paused` path, thereby acquiring/reusing the durable replacement-publication record and its leases. Variant `:01` adopts the already-created NL/FR/EN children. Variants `:02` and `:03` persist namespaced progress through a SQL callback fenced by action ID, lease owner, lease token, and unexpired lease. Retries reconstruct variant `:01` from its durable base IDs and adopt namespaced IDs for later variants.

The saga verifies all three paused bundles, performs guarded activation across all nine ads, verifies all three active bundles, and only then pauses the source. Failure after an external write enters reverse variant cleanup and requires all three bundles to reread paused; unproven cleanup is returned through `_SagaFailure`, causing the existing executor reconciliation block rather than a clean retry. Shadow mode still refuses before publication or Meta mutation.

Remediation checks:

- `ruff check` passed for executor, replacement, Meta adapter, and focused tests.
- Hook matrix tests: `4 passed`.
- Executor shadow-boundary tests: `2 passed`.
- No external Meta boundary, deployment, dispatch, or GitHub variable was used.

## Critical identity and recovery fixwave

- Removed generic `publish_replacement_paused` resource adoption from the hook path. It now initializes only the fenced replacement-publication database intention; every creative/ad, including variant `:01`, is created from its persisted HookExperiment language bundle.
- Live matrix reads now supply creative IDs, exact landing page, exact locale payloads, and image hashes to the existing Meta identity verifier before activation. Ordinary replacement copy therefore cannot pass as hook variant `:01`.
- Every matrix progress key is persisted immediately through the action/replacement lease owner, token, and expiry fence. Partial cleanup reconstructs IDs from the database instead of relying on an in-memory completed result, preventing the previous partial-result `KeyError` class.
- Activation renews the action lease and rereads exact creative/parent/ad identity before the first write and every subsequent ad write. Drift or lease loss enters compensation.
- Success now requires a post-mutation live source reread satisfying the exact paused-source contract; otherwise the matrix is compensated and reconciliation remains fail-closed.

Fixwave verification: Ruff and Python compilation passed; hook matrix tests `4 passed`; executor shadow-boundary tests `2 passed`; `git diff --check` passed. No external or deployment mutation occurred.

## PostgreSQL production-path coverage

Added a real PostgreSQL `execute_claim` test with a canonical persisted REPLACE decision, claimed action lease, all nine append-only experiment rows, a real `MetaExecutionAdapter`, a generated replacement draft row, and the real `_replace` hook branch. Only Meta SDK-facing functions are replaced.

The test proves that:

- all nine creative payloads originate from persisted `exp:01`/`:02`/`:03` NL/FR/EN bundles and never from the ordinary replacement draft copy;
- exactly nine unique ads activate and the source pauses;
- every creative/ad ID is immediately present in fenced `autonomous_replacement_publications.progress`;
- the action finishes `succeeded` with all three variants in persisted `after_state`.

Exact focused command:

`AGENT_DB_URL=postgresql+asyncpg://postgres:test@localhost:55432/agent_test uv run pytest -q tests/test_autonomy_executor.py -k 'execute_claim_persisted_hook_experiment_creates_and_activates_exact_3x3 or hook_experiment_adapter_shadow or shadow_mode_is_impossible'`

Result: `3 passed, 85 deselected`. Ruff and `git diff --check` also passed. Existing adapter-level PostgreSQL-independent tests continue to cover durable complete retry without duplicate SDK calls, rate-limit interruption, and child-resource drift; the saga uses the same fenced progress representation exercised by the new PostgreSQL success path.
