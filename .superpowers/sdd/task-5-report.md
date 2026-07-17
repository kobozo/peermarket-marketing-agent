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

## Replacement-publication lease fencing

Creation now checks the intention UPSERT rowcount and then locks/selects the replacement publication only when action ID, lease owner, lease token, and unexpired lease all match. Cleanup independently renews the action and selects progress with the same replacement-lease predicate. A transferred publication lease therefore causes zero matrix-create and zero reverse-pause SDK calls.

PostgreSQL coverage creates a future-dated replacement lease owned by another worker and proves both creation and cleanup refuse before their SDK boundaries.

Exact clean-database focused command:

`AGENT_DB_URL=postgresql+asyncpg://postgres:test@localhost:55432/agent_test uv run pytest -q tests/test_autonomy_executor.py -k 'execute_claim_persisted_hook_experiment_creates_and_activates_exact_3x3 or hook_creation_and_cleanup_refuse_transferred_replacement_lease_before_sdk or hook_experiment_adapter_shadow or shadow_mode_is_impossible'`

Result: `4 passed, 85 deselected in 3.08s`. Ruff passed and the diff was whitespace-clean.

## PostgreSQL failure and resume matrix

The production-path test is now parameterized across success and four injected SDK failures while retaining the real claimed action, real persisted nine-row experiment, real replacement-publication progress, real `MetaExecutionAdapter`, real `_replace`, and real executor finalization:

- rate limit after variant `:01` progress: reverse cleanup runs, action reconciles, then the same durable action/publication is re-leased to a new worker and a fresh adapter; retry succeeds with exactly nine unique creative SDK payloads total, proving variant `:01` adoption without duplicate creation;
- persisted payload versus live creative drift: the verifier receives and asserts exact creative IDs, landing URL, and NL/FR/EN locale payloads, then drift causes cleanup and reconciliation before activation;
- action lease loss during activation: the next fenced write stops and stale-worker cleanup cannot mutate;
- reverse-pause failure: cleanup is unproven and the action persists reconciliation-required rather than success or retry;
- success: all nine activate, source pauses, every ID remains in fenced DB progress, and the final action audit state succeeds.

Combined clean-DB command:

`AGENT_DB_URL=postgresql+asyncpg://postgres:test@localhost:55432/agent_test uv run pytest -q tests/test_autonomy_executor.py -k 'execute_claim_persisted_hook_experiment_creates_and_activates_exact_3x3 or hook_creation_and_cleanup_refuse_transferred_replacement_lease_before_sdk or hook_experiment_adapter_shadow or shadow_mode_is_impossible'`

Result: `8 passed, 85 deselected in 5.83s`. Ruff format/check and `git diff --check` passed.

## Per-mutation activation and cleanup fences

The former combined campaign/ad-set/first-ad activation was split into three single-resource SDK operations. Immediately before each, the adapter renews the action, verifies exact replacement-publication owner/token/expiry, and rereads the complete live creative/landing/locale hierarchy. Every remaining ad write uses the same fence. Reverse cleanup likewise renews and revalidates both leases before each variant pause.

PostgreSQL fault injection now steals leases at the exact campaign→ad-set boundary, ad-set→first-ad boundary, a later-ad boundary, and between reverse variant pauses. Each case proves no later stale write occurs and finalization remains fail-closed.

Combined command result: `11 passed, 85 deselected in 7.28s`. Ruff format/check and `git diff --check` passed.

## Live-verified reverse cleanup

Before every reverse pause, after both lease fences, cleanup reconstructs the persisted variant's exact creative/ad IDs and invokes the live verifier with the frozen landing page and NL/FR/EN payload matrix. Missing partial identity or any parent/creative/content drift prevents that pause entirely and leaves reconciliation required.

Cleanup verification now consumes the real adapter shape: `pause_errors` must be empty, `observed` must be non-empty, and every observed configured/effective state must be paused or an accepted paused/review state. Tests cover verified real-shape success, explicit pause errors, and an ambiguous ACTIVE observation. Creative drift asserts zero pause SDK calls.

Combined clean-DB result: `12 passed, 85 deselected in 17.32s`. Ruff format/check and `git diff --check` passed.
