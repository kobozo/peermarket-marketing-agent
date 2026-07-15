# Slack Thread Draft Revisions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn human replies in a Slack draft thread into audited revision variants that require fresh explicit approval.

**Architecture:** Persist Slack root bindings and revision lineage in PostgreSQL, ingest thread feedback idempotently, debounce and serialize it per root draft, regenerate through an action-aware revision service and existing quality gate, and deliver the new approval through a transactional Slack outbox. Existing approval/publication paths remain authoritative and only the newest queued variant can be decided.

**Tech Stack:** Python 3.12, Slack Bolt Socket Mode, Slack Web API, asyncio, SQLAlchemy/PostgreSQL, Anthropic client, pytest, GitHub Actions.

## Global Constraints

- A thread reply never approves or publishes unless it matches explicit `✅ <id>` or `❌ <id>` syntax.
- Every revised paid draft requires fresh explicit approval.
- Original copy, feedback, lineage, metadata, and decisions remain auditable.
- Only the latest queued variant in a root thread can be approved or rejected.
- Slack/API delivery failures never corrupt draft state and never regenerate copy twice.
- No new secrets; deployment only through GitHub Actions.

---

### Task 1: Revision Schema and Repository

**Files:**
- Modify: `src/peermarket_agent/db/migrations.py`
- Modify: `src/peermarket_agent/drafts.py`
- Create: `src/peermarket_agent/revisions.py`
- Modify: `tests/test_migrations.py`
- Create: `tests/test_revisions.py`

**Interfaces:**
- `bind_draft_thread(engine, draft_id, channel_id, root_ts)`
- `record_revision_feedback(engine, event) -> bool`
- `claim_feedback_batch(engine, root_ts) -> FeedbackBatch | None`
- `persist_revision_and_supersede(engine, predecessor_id, revised_draft, feedback_ids) -> int`

- [ ] Write failing migration tests for lineage columns, `superseded` status, unique root binding, feedback-event idempotency, and outbox uniqueness.
- [ ] Run `AGENT_DB_URL=... uv run pytest tests/test_migrations.py tests/test_revisions.py -q` and verify RED.
- [ ] Add idempotent migrations and typed repository functions; use advisory locks and conditional rowcounts for predecessor/latest invariants.
- [ ] Test concurrent feedback claims, ordered batches, atomic new-draft/supersede, duplicate Slack delivery, and rollback on invalid predecessor.
- [ ] Run focused tests, full suite, Ruff format/lint, commit `feat: persist Slack draft revision lineage`.

### Task 2: Outbound Root Binding and Transactional Outbox

**Files:**
- Modify: `src/peermarket_agent/slack_notifier.py`
- Modify: `src/peermarket_agent/agent/loops/daily.py`
- Modify: `src/peermarket_agent/slack_dm.py`
- Create: `src/peermarket_agent/slack_outbox.py`
- Create: `src/peermarket_agent/agent/loops/slack_outbox.py`
- Modify: `src/peermarket_agent/agent/main.py`
- Modify: `tests/test_slack_dm.py`
- Create: `tests/test_slack_outbox.py`
- Modify: `tests/test_loops_daily.py`

**Interfaces:**
- `SlackMessageResult(channel_id: str, ts: str)` returned for successful sends.
- `enqueue_root_approval(...)`, `enqueue_thread_approval(...)`, `deliver_pending_outbox(...)`.

- [ ] Write failing notifier tests proving Slack response channel/ts is returned without breaking existing best-effort alerts.
- [ ] Write failing outbox tests for root/thread delivery, retry, idempotency, and root binding after successful post only.
- [ ] Implement separate explicit message-send result while preserving boolean alert behavior for operational notifications.
- [ ] Change daily drafts to enqueue approval roots; an outbox delivery stores the authoritative root timestamp.
- [ ] Add hourly outbox retry and revised copy formatting with revision/change summary and explicit new-ID decisions.
- [ ] Run focused/full verification and commit `feat: bind Slack approval threads through outbox`.

### Task 3: Thread Event Routing and Feedback Debounce

**Files:**
- Modify: `src/peermarket_agent/slack_bridge/app.py`
- Create: `src/peermarket_agent/slack_bridge/revision_handler.py`
- Modify: `tests/test_slack_bridge.py`
- Create: `tests/test_revision_handler.py`

**Interfaces:**
- `handle_revision_reply(engine, event) -> RevisionReplyResult`
- Existing `parse_ack` retains precedence.

- [ ] Write failing routing tests for human DM threads, ack precedence, unknown roots, bots, edits/deletes, broadcasts, empty text, and non-DM channels.
- [ ] Write failing idempotency/debounce tests using Slack event/message timestamp keys and 15-second batch cutoff.
- [ ] Route known thread prose to feedback storage, immediately acknowledge receipt best-effort in-thread, and return without general hello text.
- [ ] Ensure unknown roots explain non-mutation and event redelivery does not enqueue generation twice.
- [ ] Run focused/full verification and commit `feat: route Slack thread replies as revision feedback`.

### Task 4: Action-Aware Revision Generation

**Files:**
- Create: `src/peermarket_agent/prompts/draft_revision.py`
- Create: `src/peermarket_agent/revision_generator.py`
- Create: `src/peermarket_agent/agent/loops/revisions.py`
- Modify: `src/peermarket_agent/agent/main.py`
- Create: `tests/test_prompt_revision.py`
- Create: `tests/test_revision_generator.py`
- Create: `tests/test_loops_revisions.py`

**Interfaces:**
- `revise_draft(claude, source_draft, feedback) -> RevisedDraft`
- `run_pending_revisions(engine, claude, notifier) -> int`

- [ ] Write failing prompt tests that delimit source/feedback as data, preserve language/action schema, forbid implicit approval, and request a structured change summary.
- [ ] Write failing schema tests for TikTok, email, SEO, and Meta structured metadata; Meta budget changes remain draft metadata only.
- [ ] Reuse brand voice and `score_draft`; score below 80 or malformed output records failure without superseding.
- [ ] Persist a valid revised draft and supersede predecessor atomically, then enqueue the same-thread approval.
- [ ] Test simultaneous feedback during generation becomes the next batch and never mutates the in-flight prompt.
- [ ] Run focused/full verification and commit `feat: generate reviewed variants from Slack feedback`.

### Task 5: Latest-Variant Approval Enforcement

**Files:**
- Modify: `src/peermarket_agent/slack_bridge/ack_handler.py`
- Modify: `tests/test_ack_handler.py`
- Modify: `src/peermarket_agent/slack_dm.py`

**Interfaces:**
- Existing `handle_ack` conditionally transitions only the latest queued draft for a revision root.

- [ ] Write failing tests that approval/rejection of superseded or non-latest queued variants is refused without publication dispatch.
- [ ] Test latest revised Meta approval invokes the existing transactional Meta pipeline exactly once.
- [ ] Add conditional latest-row validation in the same decision transaction and return a message linking to the newest draft ID.
- [ ] Run focused/full verification and commit `fix: approve only latest Slack draft variants`.

### Task 6: Whole-Feature Review and CI Deployment

**Files:**
- Modify: `README.md`
- Modify: `.github/workflows/deploy.yml` only if a new non-secret interval variable is required.

- [ ] Document thread semantics, explicit ack precedence, revision lineage, outbox retry, and operational recovery.
- [ ] Run `AGENT_DB_URL=... uv run pytest -q`, repository-wide Ruff format/lint, YAML parsing, and `git diff --check`.
- [ ] Dispatch a whole-feature review and fix every Critical/Important finding.
- [ ] Push a dedicated branch and open a draft PR; wait for GitHub CI.
- [ ] Merge only when checks pass; monitor test-gated deploy, service restart, and healthcheck.
- [ ] Send one controlled thread reply to a newly posted test draft and verify original → superseded → revised queued lineage without approving or spending.
