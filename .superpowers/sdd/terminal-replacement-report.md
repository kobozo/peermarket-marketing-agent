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

## Review amendment

Follow-up review findings were addressed test-first:

- all locally checkable prerequisites now run before the history/current-ID transition: activation enabled, structurally complete metadata, complete connector configuration, a positive whole-euro frozen budget, terminal status reads, and screenshot/image preparation;
- invalid frozen cents such as `1050` refuse without status reads, database mutation, or Meta creation, so the connector never receives a rounded value;
- the prepared image is passed into the post-transition normal pipeline, avoiding a second fallible screenshot/edit operation;
- every started replacement has one UUID-addressed, timestamped history object containing old IDs/statuses and finalized replacement IDs/state/failure;
- the finalizer runs in `finally` for handled failures, unexpected connector exceptions, and notification exceptions;
- handled partial creation failures now return a sanitized operational CLI error rather than a successful exit, while partial current IDs and failure history remain durable;
- refusal and result Slack messages are best-effort and truthful, while CLI operational errors contain no raw exception text or traceback.

Review RED cases reproduced prerequisite mutation, non-integral budget handling, missing refusal notifications, split history association, successful CLI exit on partial failure, raw unexpected connector errors, and notifier exceptions. The post-review full verification result is recorded in the final handoff.

## Final notification and history review

The final review was addressed with additional RED/GREEN coverage:

- the normal lifecycle's success notification is best-effort after `_mark_published`; notification delivery exceptions are logged and cannot downgrade or unwind verified Meta/database success;
- a real replacement lifecycle test (without mocking `_process_approved_meta_draft`) verifies that a raising notifier leaves the publication `active`, the draft `published`, and the single replacement attempt finalized successfully with current IDs;
- replacement summary notification is likewise best-effort after a successful lifecycle, so it cannot recast success as replacement failure;
- history finalization now requires both the publication row and matching `attempt_id`; missing publications and mismatched attempt IDs raise `MetaReplacementHistoryError`;
- the replacement wrapper sanitizes history-finalizer validation failures into an operator-facing operational error without exposing internal details.
