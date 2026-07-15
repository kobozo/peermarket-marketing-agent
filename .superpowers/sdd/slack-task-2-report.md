# Slack Thread Draft Revisions — Task 2 Report

## Status

DONE

## Implementation

- Added `SlackMessageResult` and an explicit `send_message` interface that
  returns Slack's authoritative channel and timestamp while preserving
  `notify_founder`'s existing best-effort boolean behavior.
- Added idempotent root and thread approval enqueue functions with immutable
  JSON payloads.
- Added due-message delivery with row locking, frozen-payload retry, failure
  categorization, and one-hour retry scheduling.
- Root drafts are bound only after Slack successfully returns a channel and
  timestamp. Thread messages use the persisted channel/root pair.
- Daily draft generation now commits approval roots to the outbox instead of
  coupling draft creation to Slack availability. The operational morning
  summary remains best effort.
- Added startup and hourly outbox delivery; the hourly delivery is isolated
  from KPI pulse failures.
- Added revised-draft formatting with complete copy, revision number, change
  summary, and explicit decisions for the new draft ID.

No inbound event routing or revision generation behavior was implemented.

## TDD evidence

Initial RED:

```text
AGENT_DB_URL=postgresql+asyncpg://postgres:test@localhost:55432/agent_test \
  uv run pytest tests/test_slack_notifier.py tests/test_slack_dm.py \
  tests/test_slack_outbox.py tests/test_loops_daily.py -q

3 collection errors: missing SlackMessageResult, revised formatter, and outbox module
```

Behavioral RED after the interfaces existed:

```text
5 failed, 11 passed
```

The failures demonstrated legacy notifier compatibility, durable enqueue,
root/thread delivery, and daily-loop integration before their fixes.

Hourly loop RED/GREEN:

```text
RED: ModuleNotFoundError: peermarket_agent.agent.loops.slack_outbox
GREEN: 1 passed in 0.23s
```

Focused GREEN:

```text
16 passed in 1.62s
```

## Verification

Final verification commands and results are recorded in the task handoff.

## Self-review

- Re-enqueueing the same idempotency key cannot replace the original text.
- Failed Slack calls do not bind a draft and retain the same stored payload.
- Delivered rows are excluded from later delivery passes.
- Operational notifications continue to swallow Slack exceptions and return
  `False`; outbox sends propagate failures so they can be recorded.
- Logs contain database IDs and exception categories, not message bodies or
  credentials.

## Concerns

Slack cannot provide exactly-once delivery across an ambiguous network timeout:
if Slack accepts a post but the client receives no response, a later retry can
duplicate the visible message. The database path is idempotent and never
regenerates copy, but Slack's `chat.postMessage` API does not expose a native
idempotency token for eliminating that external ambiguity.
