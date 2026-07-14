# Terminal-resource replacement report

## Status

Implemented the approved explicit terminal-resource replacement amendment on `feat/meta-terminal-replacement`.

The generic operator entry point is:

```text
peermarket-meta replace-terminal-draft \
  --draft-id 156 \
  --campaign-id <exact-stored-id> \
  --adset-id <exact-stored-id> \
  --creative-id <exact-stored-id> \
  --ad-id <exact-stored-id>
```

There is deliberately no budget option.

## Safety behavior

- Uses the existing per-draft PostgreSQL advisory lock.
- Requires all four supplied IDs to exactly equal the stored current IDs.
- Reads campaign, ad-set, and ad configured/effective status through the existing connector.
- Accepts only `ARCHIVED` and `DELETED` for both status fields on all three resources.
- Refuses missing, unknown, mixed terminal/nonterminal, and nonterminal state before any database mutation or creation call.
- Requires an already-frozen approved budget and passes it unchanged into the normal lifecycle.
- Atomically appends the old IDs/statuses to JSONB history and clears current IDs while locked.
- Runs the normal paused creation, activation, verification, failure rollback, and finalization path exactly once.
- Snapshots successful or partial replacement IDs/state/failure into history. Partial IDs remain the current reconciliation IDs, so the existing incomplete-ID guard prevents automatic duplicate creation.
- Emits CLI and Slack summaries containing both archived and current replacement state.

## TDD evidence

Observed RED failures before implementation:

- publication tests failed import for missing `begin_meta_terminal_replacement`;
- pipeline tests failed import for missing `replace_terminal_meta_draft`;
- partial-failure test failed because Slack did not yet identify old/current IDs.

Observed GREEN checks:

- focused persistence tests: `2 passed`;
- terminal refusal/success CLI and pipeline tests: `5 passed`;
- partial and concurrent replacement tests: `2 passed`;
- Meta/publication/CLI/pipeline regression set: `76 passed`.

## Final verification

- `AGENT_DB_URL=postgresql+asyncpg://postgres:test@localhost:55432/agent_test uv run pytest -q` → `203 passed in 14.33s`
- `uv run ruff format --check src tests` → 70 files already formatted
- `uv run ruff check src tests` → all checks passed
- YAML safe-load validation → passed
- `git diff --check` → passed

## Concerns

The command has not been run against production Meta resources. Production use still requires the operator to copy the exact stored IDs and should begin with a database read confirming draft 156's frozen budget and current hierarchy. Meta read failures refuse replacement without disclosing raw connector exceptions.
