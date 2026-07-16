# peermarket-marketing-agent

Self-extending marketing agent for PeerMarket. Runs on Proxmox VM 129 (`agent-peermarket`).

See spec: `kobozo/secondhand → docs/superpowers/specs/2026-05-23-marketing-agent-design.md`.

## Local dev

```bash
uv sync --all-extras
docker run --rm -d --name agent-test-db -p 55432:5432 \
  -e POSTGRES_PASSWORD=test -e POSTGRES_DB=agent_test \
  pgvector/pgvector:pg15
export AGENT_DB_URL=postgresql+asyncpg://postgres:test@localhost:55432/agent_test
uv run pytest -v
```

## Slack draft revisions

Draft approval messages are durable Slack thread roots. The agent records the
DM channel and root timestamp only after Slack accepts the root message. A
plain-text founder reply in that known thread is revision feedback; replies to
unknown roots, bot messages, edits, deletes, broadcasts, empty messages, and
messages outside the founder's DM are ignored or receive a non-mutating
explanation.

Explicit founder decisions always take precedence over revision prose. A reply
that matches `✅ <draft-id>` or `❌ <draft-id>` is handled as an approval or
rejection even when posted in a thread. Only the configured founder can submit
feedback or decisions, and only the latest queued variant in a thread can be
decided. Feedback never grants permission to publish or spend.

Each successful revision is a new, auditable draft. `parent_draft_id` points to
the preceding variant, `root_draft_id` remains the first draft in the thread,
and `revision_number` increases along the lineage. Persisting the new queued
variant, marking its predecessor `superseded`, applying its feedback records,
and enqueueing its Slack approval reply happen in one database transaction.
Prior variants remain immutable; a `superseded` variant cannot be approved or
published.

The first valid reply opens a 15-second debounce window. Further replies in the
same thread are stored idempotently and combined in Slack timestamp order. A
database generation lease serializes work per root draft, so concurrent workers
cannot generate two variants from the same batch. Feedback received during an
active generation remains pending for the next batch.

Slack delivery uses a transactional outbox. Root approvals and revised thread
approvals are claimed with leases and retried by the agent's hourly loop after
transient failures. A failed thread post does not regenerate or roll back the
already-persisted revision; retry sends the same outbox record to the same
thread. Root bindings are committed only after a successful Slack post.

### Recovery and observability

- Confirm both `marketing-agent.service` and `slack-bridge.service` are active;
  the former runs revision generation and outbox retries, while the latter
  ingests Socket Mode DM events.
- Inspect structured logs for feedback event ID, draft/root identifiers,
  revision number, processing attempt, lease state, and sanitized failure
  category. Credentials, raw model output, and founder PII must not be logged.
- For a missing root approval, inspect the `slack_outbox` row and its
  `status`, `next_attempt_at`, `last_failure_category`, and lease columns. Do
  not manually bind a guessed Slack timestamp; allow retry to establish it.
- For a stuck revision, inspect `draft_revision_feedback` and
  `draft_revision_generation_leases`. Expired processing/generation leases are
  reclaimed automatically. Preserve pending feedback and lineage instead of
  deleting or replaying Slack events.
- Permanent schema or brand-validation failures remain `failed` for inspection.
  A controlled operator retry uses `retry_failed_feedback(engine, (<id>,))`;
  it only transitions retained failed rows back to `pending`. Provider, network,
  and database operational failures retry automatically three times with backoff.
- Approval outbox rows are revalidated immediately before Slack posting. If the
  target was approved, rejected, or superseded after enqueue, the row becomes
  terminal `obsolete` without an API call. A decision racing after the final
  check can still coincide with an already-sent message; Slack has no atomic
  transaction with PostgreSQL, so the stored result records the observed truth.
- After recovery, verify the lineage is `queued -> superseded -> queued` and
  that the newest draft has one pending or delivered thread-approval outbox
  record. Do not approve a recovery test draft or activate paid media.

Deployment continues through the existing GitHub Actions workflow. It requires
the existing `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_SIGNING_SECRET`, and
`SLACK_FOUNDER_USER_ID` secrets; this feature adds no interval variable or new
secret. The Slack app must subscribe to direct-message `message` events and
Socket Mode must remain enabled.

### Replacing a terminal Meta hierarchy

Before running any production replacement command, deploy this capability through
a reviewed pull request and the complete GitHub Actions deployment workflow. Do
not run the command from an unmerged branch or an ad-hoc production checkout, and
never hand-edit production PostgreSQL to enable, reset, or repair a replacement.

`peermarket-meta replace-terminal-draft` is an explicit recovery command for an
`approved` or already `published` Meta draft. Use it only when the exact stored
campaign, ad set, creative, and ad IDs are present and the entire live hierarchy
is terminal (`ARCHIVED` or `DELETED`). It preserves the publication's frozen
budget; after successful activation, the replacement creates spend.

Operators must first inspect both Meta and PostgreSQL, then supply all four IDs
exactly as stored:

```bash
peermarket-meta replace-terminal-draft \
  --draft-id 156 \
  --campaign-id 120249110304880342 \
  --adset-id 120249110305000342 \
  --creative-id 28047843224854442 \
  --ad-id 120249110305530342
```

Do not blindly retry this command after a failure. Inspect the current IDs,
draft status, publication state, and finalized replacement-history entry before
deciding on any further operator action.

### Staged Meta performance rollout

Deploy performance collection disabled first. The workflow defaults both
`META_INSIGHTS_ENABLED` and `PEERMARKET_ATTRIBUTION_ENABLED` to `false`; keep
those repository variables false through the initial deployment. The remaining
repository-variable defaults are a 3-day Insights lookback, the
`Europe/Brussels` Meta account timezone, a 2-hour no-delivery grace period, a
dedicated 2-hour snapshot freshness limit, and learning minimums of 1,000
impressions, 30 landing-page views, and 10 registrations. These are operational
controls, not secrets. Insights exclude the current partial account day and
store account-calendar plus UTC-overlap identity for aggregate joins.

After the reviewed workflow has deployed, use the read-only verifier against a
known published draft:

```bash
peermarket-performance verify --draft-id 156
```

The command reads the publication, live Meta statuses and Insights, the
aggregate-only attribution view, and stored snapshot freshness. Its JSON output
is limited to IDs, statuses, counts, availability/freshness checks, and feature
flags; it never prints credentials or raw attribution rows and never changes a
Meta resource or database row.

Enable `META_INSIGHTS_ENABLED` first, redeploy through GitHub Actions, wait for
one scheduled collection, and verify that `meta_available` and `snapshot_fresh`
are true. Review delivery counts before enabling
`PEERMARKET_ATTRIBUTION_ENABLED`, redeploy again, and verify aggregate-view
availability. Leave either flag false if its check is unavailable. Do not lower
learning thresholds or enable paid-media behavior merely to make a rollout
check pass; adjust thresholds only in a separate reviewed change backed by
sufficient production evidence.
