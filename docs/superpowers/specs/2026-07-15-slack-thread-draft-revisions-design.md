# Slack Thread Draft Revisions Design

## Goal

A human reply inside the Slack thread of a draft approval message is a request
to revise that draft. The agent preserves the original, turns the feedback into
one new variant, and posts that variant back into the same thread for a fresh,
explicit approval decision.

## Message interpretation

Only a human-authored message with a non-empty `thread_ts` in the founder's
direct-message channel can request a revision. The root `thread_ts` must map to
an approval draft sent by this agent. Bot messages, message edits/deletes,
channel broadcasts, files without text, messages in other channels, and replies
to unknown roots are ignored or receive a non-mutating explanation.

Explicit approval/rejection syntax remains authoritative. `✅ <draft-id>` and
`❌ <draft-id>` are routed to the existing acknowledgement handler even when
posted in a thread; they are never interpreted as revision prose. Any other
non-empty human reply in a known approval thread is revision feedback.

## Thread and revision identity

When the agent posts a root approval message, it stores Slack channel ID and
root message timestamp against that draft. A revised draft inherits the same
thread identity and has:

- `parent_draft_id`, pointing to the immediately preceding variant;
- `root_draft_id`, pointing to the first variant;
- a monotonically increasing `revision_number`;
- the exact human feedback and Slack message timestamp;
- status `queued` until explicitly approved or rejected.

The original and prior variants are never edited or deleted. Once a replacement
variant passes validation and is persisted, its predecessor becomes
`superseded`. `superseded` drafts cannot be approved or published.

## Feedback collection and concurrency

The first valid feedback reply starts a short debounce window of 15 seconds.
Additional human replies in the same thread during that window are stored in
Slack timestamp order and combined as separate instructions. This prevents one
new variant per sentence when the founder sends several consecutive messages.

One PostgreSQL advisory lock per root draft serializes revision generation.
Slack event IDs/message timestamps are unique idempotency keys. Redelivered
events do not regenerate. If feedback arrives while generation is already
running, it becomes the next feedback batch rather than modifying the in-flight
prompt.

## Revision generation

The revision service loads the latest queued variant in the thread, its action
type, language, copy, structured metadata, brand voice, and the ordered feedback
batch. Claude receives an explicit revision prompt that:

- treats the stored draft as source material rather than instructions;
- applies only the founder's requested changes;
- preserves unaffected facts, language, channel, audience, CTA, and approved
  budget unless the feedback explicitly requests a permitted change;
- produces the same action-type-specific structured schema as normal
  generation;
- never treats feedback as approval or permission to publish.

For paid Meta drafts, a requested budget change is represented in the new draft
metadata but still requires fresh approval. It cannot alter an already approved
or published campaign.

The revised result runs through the existing schema validation, truthfulness
rules, and brand-quality gate. A score below 80 or invalid structured output
does not supersede the current draft. The agent posts a concise failure in the
thread and retains the feedback for a manual or subsequent retry.

## Posting and approval

After successful persistence, the agent posts in the same thread:

- the new draft number and revision number;
- the complete revised copy and relevant structured fields;
- a concise summary of requested changes applied;
- explicit `✅ <new-id>` and `❌ <new-id>` instructions.

Only the latest queued variant for a thread can be approved. Approving a newer
variant uses the existing channel-specific publication path. Approval never
cascades from the predecessor. Rejecting the latest variant marks only that
variant rejected; prior superseded variants remain immutable.

## Failure handling

Slack acknowledgement of received feedback is best-effort and cannot change
draft state. Generation/API failure leaves the predecessor queued and records a
sanitized revision failure. Database persistence of the new variant and
superseding its predecessor occur atomically. If posting the new approval
message fails after persistence, the new variant remains queued and an hourly
outbox retry reposts it to the same thread without regenerating.

No raw Claude response, token, Slack credential, email address, or other PII is
written to logs. Logs use draft IDs, root timestamp hash/identifier, revision
number, event ID, and failure category.

## Data model

The agent database gains revision lineage columns on `drafts`, a unique Slack
root binding, and two focused tables:

- `draft_revision_feedback`: idempotent Slack feedback events and processing
  state (`pending`, `processing`, `applied`, `failed`);
- `slack_outbox`: idempotent root/thread messages awaiting delivery.

All migrations are idempotent and reconcile existing drafts without Slack
bindings. Existing approval messages cannot accept revision prose until a root
binding is known; the agent explains this rather than guessing a draft from
message text.

## Testing and deployment

Tests cover root-message timestamp persistence, thread routing, ack precedence,
unknown roots, bot/edit redelivery filtering, debounce ordering, concurrent
feedback, idempotency, lineage, atomic superseding, schema/brand-gate failure,
Meta structured metadata and budget behavior, latest-only approval, outbox
retry, and Slack failure after persistence.

Deployment uses the existing CI workflow and GitHub secrets. No new secret is
required. Socket Mode must subscribe to direct-message `message` events with
thread metadata, which is already the event family consumed by the bridge.
