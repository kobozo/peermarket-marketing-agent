# Task 6 fourth fix report

## Commit

- `697f61c fix: verify replacement rollback readback`

## Root cause

The source-pause compensation branch raised immediately after its rollback/readback `try`, leaving `_bundle_verified` and rollback audit construction unreachable. Existing replacement tests invoked `_replace` directly and mocked lease renewal, so persisted public lifecycle behavior was not proven.

## RED

```bash
AGENT_DB_URL='postgresql+asyncpg://postgres:test@localhost:55432/autonomy_test' uv run pytest -q tests/test_autonomy_executor.py::test_public_replace_source_pause_unverified_rollback_persists_exact_audit
```

```text
FAILED: expected failure_category replacement_rollback_unproven,
observed external_state_unproven
1 failed in 2.26s
```

The test uses `enqueue_action`, `claim_next_action`, PostgreSQL migrations, and public `execute_claim`; it does not mock `_renew_action`.

## GREEN

```text
1 passed in 1.84s
```

The persisted action now records `replacement_rollback_unproven`, the same exact failure message, and rollback audit containing the observed bundle and `verified: false`.

## Verification

```bash
AGENT_DB_URL='postgresql+asyncpg://postgres:test@localhost:55432/autonomy_test' uv run pytest -q tests/test_autonomy_executor.py
# 26 passed in 12.09s

uv run ruff check src/peermarket_agent/autonomy/executor.py tests/test_autonomy_executor.py
# All checks passed!

AGENT_DB_URL='postgresql+asyncpg://postgres:test@localhost:55432/autonomy_test' uv run pytest -q tests/test_autonomy_executor.py tests/test_meta_ads.py tests/test_meta_pipeline.py tests/test_autonomy_store.py
# 194 passed in 43.35s

git diff --check
AGENT_DB_URL='postgresql+asyncpg://postgres:test@localhost:55432/autonomy_test' uv run pytest -q
# 753 passed in 105.69s (0:01:45)
```

## Remaining review scope

The requested exhaustive public-entrypoint matrix is not fully represented yet. In particular, several legacy replacement tests still call `_replace` directly and mock `_renew_action`, and separate public tests for all reallocation write/rollback boundaries, execution-time races, lease-loss boundaries, retry/adoption, and reconciliation blocking remain to be converted or added.
