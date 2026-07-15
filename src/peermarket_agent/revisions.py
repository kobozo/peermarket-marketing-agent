"""Persistence primitives for Slack-thread draft revisions."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from peermarket_agent.drafts import Draft, draft_insert_params


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
) -> FeedbackBatch | None:
    cutoff = (now or datetime.now(UTC)) - timedelta(seconds=15)
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
                "UPDATE draft_revision_feedback SET status = 'processing', claimed_at = NOW() "
                "WHERE id = ANY(:ids) AND status = 'pending'"
            ),
            {"ids": list(feedback_ids)},
        )
        if result.rowcount != len(feedback_ids):
            raise RuntimeError("feedback claim changed concurrently")
    return FeedbackBatch(
        root_draft_id=int(root),
        feedback_ids=feedback_ids,
        instructions=tuple(str(row[1]) for row in rows),
    )


async def persist_revision_and_supersede(
    engine: AsyncEngine,
    predecessor_id: int,
    revised_draft: Draft,
    feedback_ids: tuple[int, ...],
) -> int:
    if not feedback_ids:
        raise ValueError("revision requires claimed feedback")
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
            raise ValueError("draft is not the latest queued predecessor")
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
                    "SELECT id, feedback_text, message_ts FROM draft_revision_feedback "
                    "WHERE id = ANY(:ids) AND root_draft_id = :root "
                    "AND status = 'processing' ORDER BY message_ts, id FOR UPDATE"
                ),
                {"ids": list(feedback_ids), "root": root_id},
            )
        ).fetchall()
        if len(feedback) != len(set(feedback_ids)):
            raise ValueError("revision feedback is not claimed for this root")
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
            raise ValueError("draft is not the latest queued predecessor")
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
    return int(new_id)
