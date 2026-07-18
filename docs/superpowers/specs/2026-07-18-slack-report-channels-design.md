# Design: Per-platform Slack report channels + Block Kit styling

Date: 2026-07-18
Status: approved (Yannick, in-session)

## Goal

Reports currently post as flat JSON-ish text to the founder DM. Route them to the
matching per-platform Slack channel and render them as styled Block Kit messages.

Slack channels (the bot has been invited to all three):

| Domain channel | Slack channel | ID |
|---|---|---|
| `tiktok` | #tiktok | `C0BJ71Z4YFL` |
| `meta` (facebook) | #facebook | `C0BJ0PUURRR` |
| `email` | #email | `C0BHRLPM3QX` |

## Scope

Routed to platform channels (reports only):

- Daily performance summary (`agent/loops/performance_daily.py`)
- Hourly Meta KPI alert (`agent/loops/hourly.py`)
- Autonomy audit/shadow messages (`agent/loops/autonomy.py` → `slack_outbox`)

All three are Meta-scoped today, so they land in #facebook; the tiktok/email routes
are wired and ready for when those report types exist.

**Out of scope:** draft approval/revision threads and the daily draft summary stay in
the founder DM — they are part of the approval flow, not reporting.

## Design

### 1. Channel routing

- Three new optional settings in `config.py` (env-driven, default `""`):
  `SLACK_REPORT_CHANNEL_TIKTOK`, `SLACK_REPORT_CHANNEL_META`,
  `SLACK_REPORT_CHANNEL_EMAIL`. Documented in `.env.example`.
- New helper `slack_routing.py`: `report_channel_id(settings, channel) -> str | None`
  maps the domain `channel` value (`tiktok` / `meta` / `email`) to the configured
  Slack channel ID. Unknown channel or unset env var → `None`, and callers fall back
  to the founder DM. Nothing breaks when an env var is missing.

### 2. Block Kit rendering

- `SlackNotifier.send_message()` gains an optional `blocks` parameter forwarded to
  `chat_postMessage`. `text` stays as the notification-preview fallback (Slack
  requires it).
- New module `slack_blocks.py` with three pure builders (no I/O):
  - `autonomy_audit_blocks(payload)` — header ("🤖 Autonomy shadow — observe"),
    section with campaign/reason/detail, fields per variant (impressions,
    landing-page views, registrations vs. thresholds), budget change and rollback
    lines, context footer with next evaluation time. No raw JSON dumps.
  - `performance_summary_blocks(...)` — header + per-campaign sections replacing the
    plain-text `_summary` layout.
  - `hourly_alert_blocks(...)` — compact section + context footer.
- Blocks are rendered **at enqueue time** and stored in the outbox JSONB payload
  (`payload["blocks"]`), the same pattern as the pre-rendered `text`.
  `OutboxMessage` and `deliver_pending_outbox` read and forward them; old rows
  without blocks fall back to text unchanged.

### 3. Outbox / senders

- Autonomy `_audit` sets `channel_id` (routed) and `payload["blocks"]` when
  inserting the outbox row. No schema migration needed — the `channel_id` column and
  JSONB payload already exist.
- `performance_daily.py` and `hourly.py` switch from `notify_founder(text)` to
  `send_message(text, channel_id=routed, blocks=...)` with founder-DM fallback.

### 4. Error handling

- Missing/invalid channel ID at delivery → Slack API error propagates into the
  existing outbox retry path.
- Unset routing env vars → founder DM fallback, current behaviour preserved.

### 5. Testing

- Block builders: sample autonomy payload → expected block structure (header,
  fields, no JSON dumps).
- Routing helper: mapping and fallback behaviour.
- Notifier: `blocks` passthrough to `chat_postMessage`.
- Outbox: delivery forwards blocks + channel; legacy rows (text-only) still deliver.
- Autonomy enqueue: row carries routed `channel_id` and `blocks`.
- Full pytest suite green; after merge, live posting rights verified against the
  three channels with the real bot token.
