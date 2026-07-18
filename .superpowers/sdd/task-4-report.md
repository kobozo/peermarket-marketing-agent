# Task 4 Report: Outbox carries blocks + autonomy audit routing

## Status: DONE

## Implemented
1. **Outbox carries blocks** (`src/peermarket_agent/slack_outbox.py`)
   - `OutboxMessage` gained `blocks: list | None = None`.
   - `_claim_pending_outbox` maps `blocks=row["payload"].get("blocks") or None`.
   - `deliver_pending_outbox` forwards `blocks=row.blocks` to `notifier.send_message(...)`.
2. **Autonomy audit routing + Block Kit** (`src/peermarket_agent/agent/loops/autonomy.py`)
   - Imports added: `autonomy_audit_blocks` (slack_blocks), `report_channel_id` (slack_routing).
   - `_audit` signature gained `settings: Any = None`.
   - Payload literal gained `"detail": detail,` (flat `"text"` fallback unchanged).
   - After the payload literal: `payload["blocks"] = autonomy_audit_blocks(payload)` (built from the
     complete payload BEFORE "blocks" is inserted — no self-reference), then
     `channel_id = report_channel_id(settings, "meta") if settings is not None else None`.
   - INSERT now includes the `channel_id` column with param `"channel": channel_id`.
   - All 5 `_audit(` call sites inside `run_autonomy_cycle` now pass `settings=settings`.

## TDD evidence
### RED (before implementation)
4 new tests failed:
- `test_payload_blocks_are_forwarded_to_send_message` — send_message got no `blocks` kwarg.
- `test_legacy_payload_without_blocks_delivers_with_none` — same.
- `test_audit_routes_to_report_channel_and_renders_blocks` — `TypeError: _audit() got an unexpected keyword argument 'settings'`.
- `test_audit_without_report_channel_leaves_channel_null` — same TypeError.
(`4 failed in 10.98s`)

### GREEN (after implementation)
`uv run pytest tests/test_slack_outbox.py tests/test_autonomy_loop.py -q` → **36 passed in 38.02s**
`ruff check` on all changed files → **All checks passed!**

## Files changed
- `src/peermarket_agent/slack_outbox.py`
- `src/peermarket_agent/agent/loops/autonomy.py`
- `tests/test_slack_outbox.py` (added 2 tests + `import json`; updated one pre-existing exact-call
  assertion `test_thread_delivery_uses_stored_channel_and_root` to include `blocks=None`, since
  `deliver_pending_outbox` now always forwards the kwarg per the brief)
- `tests/test_autonomy_loop.py` (added 2 tests)

## Self-review
- Blocks built from payload before the "blocks" key is added — verified, no self-reference.
- Flat `"text"` fallback string left byte-for-byte unchanged.
- `report_channel_id` returns None for an empty-string setting → `channel_id IS NULL` path verified by test.
- `settings=None` default keeps backward compatibility (existing `_audit` calls without settings still pass; channel stays NULL).
- DB-backed tests ran against the live pgvector container (port 55432).

## Concerns
None. Full suite for both files passed against a real Postgres.

## Commit
`706e723 feat: route autonomy audits to report channel with Block Kit payload`
