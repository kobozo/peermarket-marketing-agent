# Task 1 Report: Contracts, Configuration, and Durable Schema

## Status

DONE

## Commit

- `b3899a4 feat: add autonomous lifecycle contracts and controls`

## Files changed

- `.github/workflows/deploy.yml`
- `src/peermarket_agent/autonomy/__init__.py`
- `src/peermarket_agent/autonomy/contracts.py`
- `src/peermarket_agent/config.py`
- `src/peermarket_agent/db/migrations.py`
- `tests/test_autonomy_contracts.py`
- `tests/test_config.py`
- `tests/test_deploy_workflow.py`
- `tests/test_migrations.py`

## TDD evidence

### Baseline

Command:

```bash
AGENT_DB_URL='postgresql+asyncpg://postgres:test@localhost:55432/autonomy_test' uv run pytest -q
```

Output:

```text
516 passed in 59.29s
```

### Initial RED

Command:

```bash
AGENT_DB_URL='postgresql+asyncpg://postgres:test@localhost:55432/autonomy_test' uv run pytest -q tests/test_autonomy_contracts.py tests/test_config.py tests/test_migrations.py tests/test_deploy_workflow.py
```

Output (expected missing-contract failure):

```text
ERROR tests/test_autonomy_contracts.py
ModuleNotFoundError: No module named 'peermarket_agent.autonomy'
1 error in 0.35s
```

### Initial GREEN

Same focused command after minimal implementation:

```text
42 passed in 2.65s
```

### Brief-signature RED/GREEN

The verbatim brief construction initially raised `TypeError` instead of the required `ValueError`:

```bash
uv run pytest -q tests/test_autonomy_contracts.py::test_frozen_decision_rejects_scale_without_budget_values
```

```text
TypeError: FrozenDecision.__init__() missing 3 required positional arguments: 'window_start', 'window_end', and 'idempotency_key'
1 failed in 0.04s
```

After moving missing-field enforcement into `__post_init__`, the focused suite passed:

```text
43 passed in 2.68s
```

### ASCII campaign-ID RED/GREEN

Self-review identified that `str.isdecimal()` accepts non-ASCII numerals. Tests using Arabic-Indic digits correctly failed first:

```bash
uv run pytest -q tests/test_autonomy_contracts.py::test_frozen_decision_requires_exact_numeric_campaign_id tests/test_config.py::test_autonomy_campaign_allowlist_rejects_invalid_ids
```

```text
2 failed, 8 passed in 0.14s
```

After restricting IDs to ASCII decimal strings:

```text
45 passed in 4.10s
```

### Final GREEN

Focused lint and tests:

```bash
uv run ruff check src/peermarket_agent/autonomy src/peermarket_agent/config.py src/peermarket_agent/db/migrations.py tests/test_autonomy_contracts.py tests/test_config.py tests/test_migrations.py tests/test_deploy_workflow.py
AGENT_DB_URL='postgresql+asyncpg://postgres:test@localhost:55432/autonomy_test' uv run pytest -q tests/test_autonomy_contracts.py tests/test_config.py tests/test_migrations.py tests/test_deploy_workflow.py
```

```text
All checks passed!
45 passed in 4.10s
```

Formatting:

```bash
uv run ruff format --check src/peermarket_agent/autonomy src/peermarket_agent/config.py src/peermarket_agent/db/migrations.py tests/test_autonomy_contracts.py tests/test_config.py tests/test_migrations.py tests/test_deploy_workflow.py
```

```text
8 files already formatted
```

Fresh full suite after all code and formatting changes:

```bash
AGENT_DB_URL='postgresql+asyncpg://postgres:test@localhost:55432/autonomy_test' uv run pytest -q
```

```text
541 passed in 66.39s (0:01:06)
```

## Implementation summary

- Added string enums for autonomous decisions and leased-action lifecycle states.
- Added a frozen, slotted decision dataclass with exact ASCII campaign ID, aware/ordered evidence window, audit input, and positive budget-action validation.
- Added safe disabled/shadow defaults, bounded controls, parsed allowlist, and live-execution allowlist validation to settings.
- Added durable decision, leased action, and budget-event tables with unique/idempotency, status, partial-index, lease, JSONB audit, cents, timestamp, and lookup constraints/indexes.
- Wired all eight autonomy repository variables into the deploy environment and generated `secrets.env`, defaulting execution off and shadow on.

## Self-review

- Re-read the Task 1 brief and checked every named file, setting, default, validation rule, schema characteristic, workflow variable, and test command.
- Confirmed the workflow uses repository variables rather than secrets for autonomy controls.
- Confirmed the partial unique index covers only `pending`, `leased`, and `executing` actions.
- Confirmed migrations execute twice successfully through the existing idempotency test.
- Confirmed no unrelated tracked files or pre-existing changes were included in the commit.
- Confirmed `git diff --check`, Ruff lint, Ruff formatting, focused tests, and the full suite are clean.

## Concerns

None.

## Review fix: deep immutability, timezone awareness, and append-only enforcement

### Fix commit

- `03db587 fix: harden autonomous decision immutability`

### Regression RED

Command:

```bash
AGENT_DB_URL='postgresql+asyncpg://postgres:test@localhost:55432/autonomy_test' uv run pytest -q tests/test_autonomy_contracts.py::test_frozen_decision_deeply_isolates_and_freezes_evidence tests/test_autonomy_contracts.py::test_frozen_decision_evidence_remains_json_serializable tests/test_autonomy_contracts.py::test_frozen_decision_rejects_tzinfo_with_no_utc_offset tests/test_migrations.py::test_autonomous_decisions_are_append_only_at_database_level
```

Result:

```text
FFFF                                                                     [100%]
FAILED tests/test_autonomy_contracts.py::test_frozen_decision_deeply_isolates_and_freezes_evidence
  assert [12, 24, 48] == [12, 24]
FAILED tests/test_autonomy_contracts.py::test_frozen_decision_evidence_remains_json_serializable
  TypeError: Object of type set is not JSON serializable
FAILED tests/test_autonomy_contracts.py::test_frozen_decision_rejects_tzinfo_with_no_utc_offset
  TypeError: can't compare offset-naive and offset-aware datetimes
FAILED tests/test_migrations.py::test_autonomous_decisions_are_append_only_at_database_level
  Failed: DID NOT RAISE <class 'Exception'>
4 failed in 0.58s
```

### Regression GREEN

Same command after the minimal implementation:

```text
....                                                                     [100%]
4 passed in 0.48s
```

### Focused verification

Commands:

```bash
uv run ruff check src/peermarket_agent/autonomy/contracts.py src/peermarket_agent/db/migrations.py tests/test_autonomy_contracts.py tests/test_migrations.py
uv run ruff format --check src/peermarket_agent/autonomy/contracts.py src/peermarket_agent/db/migrations.py tests/test_autonomy_contracts.py tests/test_migrations.py
AGENT_DB_URL='postgresql+asyncpg://postgres:test@localhost:55432/autonomy_test' uv run pytest -q tests/test_autonomy_contracts.py tests/test_config.py tests/test_migrations.py tests/test_deploy_workflow.py
```

Results:

```text
All checks passed!
4 files already formatted
49 passed in 2.86s
```

### Full verification

Command:

```bash
git diff --check
AGENT_DB_URL='postgresql+asyncpg://postgres:test@localhost:55432/autonomy_test' uv run pytest -q
```

Result:

```text
git diff --check: no output (exit 0)
545 passed in 68.73s (0:01:08)
```

### Implementation notes

- Evidence is recursively copied into mutation-rejecting `dict`/`list` subclasses; mappings, sequences, and sets are isolated while remaining directly JSON serializable for later JSONB persistence.
- Timestamp awareness now requires both a `tzinfo` and a non-`None` `utcoffset()`.
- An idempotently installed PostgreSQL trigger rejects both `UPDATE` and `DELETE` on `autonomous_decisions`; migration tests reset by dropping the schema, so append-only enforcement does not interfere with cleanup.
