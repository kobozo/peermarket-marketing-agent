"""Persistence primitives for Slack-thread draft revisions."""

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from peermarket_agent.drafts import Draft, draft_insert_params
from peermarket_agent.revision_generator import SourceDraft


@dataclass(frozen=True)
class RevisionFeedbackEvent:
    event_id: str
    channel_id: str
    root_ts: str
    message_ts: str
    text: str


@dataclass(frozen=True)
class FeedbackBatch:
    root_draft_id: int
    feedback_ids: tuple[int, ...]
    instructions: tuple[str, ...]
    lease_owner: str


class RevisionConflictError(ValueError):
    """The claimed source is no longer the latest queued leaf."""


async def list_ready_feedback_threads(engine: AsyncEngine) -> tuple[tuple[str, str], ...]:
    """Return roots whose oldest pending feedback has completed the debounce."""
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT channel_id, root_ts FROM draft_revision_feedback WHERE "
                    "(status='pending' AND received_at <= NOW()-INTERVAL '15 seconds') "
                    "OR (status='processing' AND processing_lease_expires_at <= NOW()) "
                    "GROUP BY channel_id, root_ts ORDER BY MIN(received_at)"
                )
            )
        ).fetchall()
    return tuple((str(row[0]), str(row[1])) for row in rows)


async def load_latest_revision_source(
    engine: AsyncEngine, root_draft_id: int
) -> tuple[int, SourceDraft]:
    """Load the current queued leaf used as the immutable generation source."""
    async with engine.connect() as connection:
        row = (
            (
                await connection.execute(
                    text(
                        "SELECT d.id, at.name, d.channel, d.language, d.copy, d.metadata, "
                        "d.asset_path, d.revision_number FROM drafts d JOIN action_types at "
                        "ON at.id=d.action_type_id "
                        "WHERE d.root_draft_id=:root AND d.status='queued' "
                        "ORDER BY d.revision_number DESC LIMIT 1"
                    ),
                    {"root": root_draft_id},
                )
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        raise ValueError("revision root has no queued predecessor")
    return int(row["id"]), SourceDraft(
        action_type_name=str(row["name"]),
        channel=str(row["channel"]),
        language=row["language"],
        copy=str(row["copy"]),
        metadata=dict(row["metadata"] or {}),
        asset_path=row["asset_path"],
        revision_number=int(row["revision_number"]),
    )


async def mark_feedback_failed(
    engine: AsyncEngine,
    feedback_ids: tuple[int, ...],
    failure_category: str,
    *,
    lease_owner: str | None = None,
) -> None:
    """Finalize a claimed batch as failed without exposing model output."""
    async with engine.begin() as connection:
        owner_clause = " AND processing_owner=:owner" if lease_owner else ""
        result = await connection.execute(
            text(
                "UPDATE draft_revision_feedback SET status='failed', "
                "failure_category=:category WHERE id=ANY(:ids) AND status='processing'"
                + owner_clause
            ),
            {
                "ids": list(feedback_ids),
                "category": failure_category[:100],
                "owner": lease_owner,
            },
        )
        if result.rowcount != len(set(feedback_ids)):
            raise RuntimeError("feedback failure state changed concurrently")
        if lease_owner:
            await connection.execute(
                text(
                    "DELETE FROM draft_revision_generation_leases leases USING "
                    "draft_revision_feedback feedback WHERE feedback.id=:feedback_id "
                    "AND leases.root_draft_id=feedback.root_draft_id "
                    "AND leases.lease_owner=:owner"
                ),
                {"feedback_id": feedback_ids[0], "owner": lease_owner},
            )


async def requeue_feedback_batch(
    engine: AsyncEngine, feedback_ids: tuple[int, ...], lease_owner: str
) -> None:
    """Return an owned conflicted/cancelled batch to pending and release its root."""
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE draft_revision_feedback SET status='pending', processing_owner=NULL, "
                "processing_lease_expires_at=NULL WHERE id=ANY(:ids) "
                "AND status='processing' AND processing_owner=:owner"
            ),
            {"ids": list(feedback_ids), "owner": lease_owner},
        )
        await connection.execute(
            text(
                "DELETE FROM draft_revision_generation_leases leases USING "
                "draft_revision_feedback feedback WHERE feedback.id=:feedback_id "
                "AND leases.root_draft_id=feedback.root_draft_id AND leases.lease_owner=:owner"
            ),
            {"feedback_id": feedback_ids[0], "owner": lease_owner},
        )


async def renew_generation_lease(
    engine: AsyncEngine,
    root_draft_id: int,
    lease_owner: str,
    *,
    now: datetime | None = None,
    lease_seconds: int = 300,
) -> bool:
    """Extend an owned root/feedback lease without retaining a DB connection."""
    renewal_time = now or datetime.now(UTC)
    async with engine.begin() as connection:
        renewed = await connection.execute(
            text(
                "UPDATE draft_revision_generation_leases SET lease_expires_at="
                "CAST(:now AS TIMESTAMPTZ)+make_interval(secs => :lease_seconds) "
                "WHERE root_draft_id=:root AND lease_owner=:owner"
            ),
            {
                "root": root_draft_id,
                "owner": lease_owner,
                "now": renewal_time,
                "lease_seconds": lease_seconds,
            },
        )
        if renewed.rowcount != 1:
            return False
        await connection.execute(
            text(
                "UPDATE draft_revision_feedback SET processing_lease_expires_at="
                "CAST(:now AS TIMESTAMPTZ)+make_interval(secs => :lease_seconds) "
                "WHERE root_draft_id=:root AND status='processing' "
                "AND processing_owner=:owner"
            ),
            {
                "root": root_draft_id,
                "owner": lease_owner,
                "now": renewal_time,
                "lease_seconds": lease_seconds,
            },
        )
    return True


async def bind_draft_thread(
    engine: AsyncEngine, draft_id: int, channel_id: str, root_ts: str
) -> None:
    try:
        async with engine.begin() as connection:
            result = await connection.execute(
                text(
                    "UPDATE drafts SET slack_channel_id = :channel_id, "
                    "slack_root_ts = :root_ts, root_draft_id = id "
                    "WHERE id = :draft_id AND revision_number = 0 "
                    "AND status = 'queued' AND ("
                    "(slack_channel_id IS NULL AND slack_root_ts IS NULL) OR "
                    "(slack_channel_id = :channel_id AND slack_root_ts = :root_ts))"
                ),
                {
                    "draft_id": draft_id,
                    "channel_id": channel_id,
                    "root_ts": root_ts,
                },
            )
            if result.rowcount != 1:
                raise ValueError("draft cannot be bound to this Slack thread")
    except IntegrityError as error:
        raise ValueError("Slack thread is already bound to another draft") from error


async def record_revision_feedback(engine: AsyncEngine, event: RevisionFeedbackEvent) -> bool:
    async with engine.begin() as connection:
        root = (
            await connection.execute(
                text(
                    "SELECT id FROM drafts WHERE slack_channel_id = :channel_id "
                    "AND slack_root_ts = :root_ts AND revision_number = 0"
                ),
                {"channel_id": event.channel_id, "root_ts": event.root_ts},
            )
        ).scalar_one_or_none()
        if root is None:
            return False
        result = await connection.execute(
            text(
                "INSERT INTO draft_revision_feedback "
                "(event_id, channel_id, root_ts, message_ts, feedback_text, root_draft_id) "
                "VALUES (:event_id, :channel_id, :root_ts, :message_ts, :feedback_text, :root) "
                "ON CONFLICT DO NOTHING"
            ),
            {
                "event_id": event.event_id,
                "channel_id": event.channel_id,
                "root_ts": event.root_ts,
                "message_ts": event.message_ts,
                "feedback_text": event.text,
                "root": root,
            },
        )
    return result.rowcount == 1


async def claim_feedback_batch(
    engine: AsyncEngine,
    channel_id: str,
    root_ts: str,
    *,
    now: datetime | None = None,
    owner: str | None = None,
    lease_seconds: int = 300,
) -> FeedbackBatch | None:
    claim_now = now or datetime.now(UTC)
    cutoff = claim_now - timedelta(seconds=15)
    lease_owner = owner or str(uuid.uuid4())
    async with engine.begin() as connection:
        root = (
            await connection.execute(
                text(
                    "SELECT id FROM drafts WHERE slack_channel_id = :channel_id "
                    "AND slack_root_ts = :root_ts AND revision_number = 0"
                ),
                {"channel_id": channel_id, "root_ts": root_ts},
            )
        ).scalar_one_or_none()
        if root is None:
            return None
        await connection.execute(
            text(
                "SELECT pg_advisory_xact_lock(hashtext('draft_revision_feedback'), hashint8(:root))"
            ),
            {"root": root},
        )
        acquired = (
            await connection.execute(
                text(
                    "INSERT INTO draft_revision_generation_leases "
                    "(root_draft_id, lease_owner, lease_expires_at) VALUES "
                    "(:root, :owner, CAST(:now AS TIMESTAMPTZ) + "
                    "make_interval(secs => :lease_seconds)) "
                    "ON CONFLICT (root_draft_id) DO UPDATE SET lease_owner=:owner, "
                    "lease_expires_at=CAST(:now AS TIMESTAMPTZ) + "
                    "make_interval(secs => :lease_seconds), "
                    "attempt_count=draft_revision_generation_leases.attempt_count+1 "
                    "WHERE draft_revision_generation_leases.lease_expires_at <= "
                    "CAST(:now AS TIMESTAMPTZ) "
                    "RETURNING root_draft_id"
                ),
                {
                    "root": root,
                    "owner": lease_owner,
                    "now": claim_now,
                    "lease_seconds": lease_seconds,
                },
            )
        ).scalar_one_or_none()
        if acquired is None:
            return None
        await connection.execute(
            text(
                "UPDATE draft_revision_feedback SET status='pending', processing_owner=NULL, "
                "processing_lease_expires_at=NULL WHERE root_draft_id=:root "
                "AND status='processing' AND processing_lease_expires_at <= "
                "CAST(:now AS TIMESTAMPTZ)"
            ),
            {"root": root, "now": claim_now},
        )
        oldest_pending = (
            await connection.execute(
                text(
                    "SELECT MIN(received_at) FROM draft_revision_feedback "
                    "WHERE root_draft_id = :root AND status = 'pending'"
                ),
                {"root": root},
            )
        ).scalar_one_or_none()
        if oldest_pending is None or oldest_pending > cutoff:
            await connection.execute(
                text(
                    "DELETE FROM draft_revision_generation_leases "
                    "WHERE root_draft_id=:root AND lease_owner=:owner"
                ),
                {"root": root, "owner": lease_owner},
            )
            return None
        rows = (
            await connection.execute(
                text(
                    "SELECT id, feedback_text FROM draft_revision_feedback "
                    "WHERE root_draft_id = :root AND status = 'pending' "
                    "ORDER BY message_ts, id FOR UPDATE"
                ),
                {"root": root},
            )
        ).fetchall()
        if not rows:
            return None
        feedback_ids = tuple(int(row[0]) for row in rows)
        result = await connection.execute(
            text(
                "UPDATE draft_revision_feedback SET status = 'processing', claimed_at = :now, "
                "processing_owner=:owner, processing_lease_expires_at="
                "CAST(:now AS TIMESTAMPTZ) + "
                "make_interval(secs => :lease_seconds), processing_attempts=processing_attempts+1 "
                "WHERE id = ANY(:ids) AND status = 'pending'"
            ),
            {
                "ids": list(feedback_ids),
                "owner": lease_owner,
                "now": claim_now,
                "lease_seconds": lease_seconds,
            },
        )
        if result.rowcount != len(feedback_ids):
            raise RuntimeError("feedback claim changed concurrently")
    return FeedbackBatch(
        root_draft_id=int(root),
        feedback_ids=feedback_ids,
        instructions=tuple(str(row[1]) for row in rows),
        lease_owner=lease_owner,
    )


async def persist_revision_and_supersede(
    engine: AsyncEngine,
    predecessor_id: int,
    revised_draft: Draft,
    feedback_ids: tuple[int, ...],
    *,
    outbox_text: str,
    outbox_idempotency_key: str,
    lease_owner: str | None = None,
) -> int:
    if not feedback_ids:
        raise ValueError("revision requires claimed feedback")
    if not outbox_text.strip() or not outbox_idempotency_key:
        raise ValueError("outbox payload and idempotency key must be non-empty")
    async with engine.begin() as connection:
        predecessor = (
            (
                await connection.execute(
                    text(
                        "SELECT id, root_draft_id, revision_number, slack_channel_id, "
                        "slack_root_ts FROM drafts WHERE id = :id FOR UPDATE"
                    ),
                    {"id": predecessor_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        if predecessor is None or predecessor["root_draft_id"] is None:
            raise RevisionConflictError("draft is not the latest queued predecessor")
        root_id = int(predecessor["root_draft_id"])
        await connection.execute(
            text(
                "SELECT pg_advisory_xact_lock(hashtext('draft_revision_feedback'), hashint8(:root))"
            ),
            {"root": root_id},
        )
        feedback = (
            await connection.execute(
                text(
                    "SELECT id, feedback_text, message_ts, processing_owner "
                    "FROM draft_revision_feedback "
                    "WHERE id = ANY(:ids) AND root_draft_id = :root "
                    "AND status = 'processing' ORDER BY message_ts, id FOR UPDATE"
                ),
                {"ids": list(feedback_ids), "root": root_id},
            )
        ).fetchall()
        if len(feedback) != len(set(feedback_ids)):
            raise ValueError("revision feedback is not claimed for this root")
        batch_owner = str(feedback[0][3])
        if any(str(row[3]) != batch_owner for row in feedback):
            raise ValueError("revision feedback has inconsistent processing ownership")
        if lease_owner is not None and batch_owner != lease_owner:
            raise ValueError("revision feedback is owned by another worker")
        superseded = await connection.execute(
            text(
                "UPDATE drafts SET status = 'superseded', decided_at = NOW(), "
                "decided_by = 'revision' WHERE id = :id AND status = 'queued' "
                "AND root_draft_id = :root AND NOT EXISTS ("
                "SELECT 1 FROM drafts newer WHERE newer.root_draft_id = :root "
                "AND newer.revision_number > drafts.revision_number)"
            ),
            {"id": predecessor_id, "root": root_id},
        )
        if superseded.rowcount != 1:
            raise RevisionConflictError("draft is not the latest queued predecessor")
        action_type_id = (
            await connection.execute(
                text("SELECT id FROM action_types WHERE name = :name"),
                {"name": revised_draft.action_type_name},
            )
        ).scalar_one_or_none()
        if action_type_id is None:
            raise ValueError(f"unknown action_type: {revised_draft.action_type_name!r}")
        combined_feedback = "\n\n".join(str(row[1]) for row in feedback)
        new_id = (
            await connection.execute(
                text(
                    "INSERT INTO drafts (action_type_id, channel, language, copy, asset_path, "
                    "generation_cost_cents, brand_score, visual_truthfulness_pass, metadata, "
                    "parent_draft_id, root_draft_id, revision_number, revision_feedback, "
                    "revision_feedback_ts, slack_channel_id, slack_root_ts) VALUES ("
                    ":action_type_id, :channel, :language, :copy, :asset_path, "
                    ":generation_cost_cents, :brand_score, :visual_truthfulness_pass, "
                    "CAST(:metadata AS JSONB), :parent, :root, :revision_number, "
                    ":feedback, :feedback_ts, :slack_channel, :slack_root) RETURNING id"
                ),
                {
                    "action_type_id": action_type_id,
                    **draft_insert_params(revised_draft),
                    "parent": predecessor_id,
                    "root": root_id,
                    "revision_number": int(predecessor["revision_number"]) + 1,
                    "feedback": combined_feedback,
                    "feedback_ts": str(feedback[-1][2]),
                    "slack_channel": predecessor["slack_channel_id"],
                    "slack_root": predecessor["slack_root_ts"],
                },
            )
        ).scalar_one()
        applied = await connection.execute(
            text(
                "UPDATE draft_revision_feedback SET status = 'applied', applied_at = NOW() "
                "WHERE id = ANY(:ids) AND status = 'processing'"
            ),
            {"ids": list(feedback_ids)},
        )
        if applied.rowcount != len(set(feedback_ids)):
            raise RuntimeError("feedback application changed concurrently")
        frozen_text = outbox_text.replace("{{draft_id}}", str(new_id))
        outbox = await connection.execute(
            text(
                "INSERT INTO slack_outbox (idempotency_key, draft_id, channel_id, root_ts, "
                "message_kind, payload) VALUES (:key, :draft_id, :channel, :root_ts, "
                "'thread_approval', CAST(:payload AS JSONB)) ON CONFLICT "
                "(idempotency_key) DO NOTHING"
            ),
            {
                "key": outbox_idempotency_key,
                "draft_id": new_id,
                "channel": predecessor["slack_channel_id"],
                "root_ts": predecessor["slack_root_ts"],
                "payload": json.dumps({"text": frozen_text}),
            },
        )
        if outbox.rowcount != 1:
            raise ValueError("outbox payload idempotency conflict")
        await connection.execute(
            text(
                "DELETE FROM draft_revision_generation_leases WHERE root_draft_id=:root "
                "AND lease_owner=:owner"
            ),
            {"root": root_id, "owner": batch_owner},
        )
    return int(new_id)
