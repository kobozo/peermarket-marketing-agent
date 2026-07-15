"""Durable Slack approval-message outbox."""

import json

import structlog
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncEngine

from peermarket_agent.slack_notifier import SlackNotifier

log = structlog.get_logger(__name__)


async def enqueue_root_approval(
    engine: AsyncEngine, *, draft_id: int, text: str, idempotency_key: str | None = None
) -> bool:
    """Persist one immutable root approval payload; return whether it was new."""
    key = idempotency_key or f"draft-root:{draft_id}"
    async with engine.begin() as connection:
        result = await connection.execute(
            sql_text(
                "INSERT INTO slack_outbox "
                "(idempotency_key, draft_id, message_kind, payload) "
                "SELECT :key, id, 'root_approval', CAST(:payload AS JSONB) FROM drafts "
                "WHERE id=:draft_id AND revision_number=0 AND status='queued' "
                "ON CONFLICT (idempotency_key) DO NOTHING"
            ),
            {"key": key, "draft_id": draft_id, "payload": json.dumps({"text": text})},
        )
    return result.rowcount == 1


async def enqueue_thread_approval(
    engine: AsyncEngine, *, draft_id: int, text: str, idempotency_key: str | None = None
) -> bool:
    """Persist a replacement approval addressed to its already-bound root."""
    key = idempotency_key or f"draft-revision:{draft_id}"
    async with engine.begin() as connection:
        result = await connection.execute(
            sql_text(
                "INSERT INTO slack_outbox "
                "(idempotency_key, draft_id, channel_id, root_ts, message_kind, payload) "
                "SELECT :key, id, slack_channel_id, slack_root_ts, 'thread_approval', "
                "CAST(:payload AS JSONB) FROM drafts WHERE id=:draft_id "
                "AND revision_number>0 AND status='queued' "
                "AND slack_channel_id IS NOT NULL AND slack_root_ts IS NOT NULL "
                "ON CONFLICT (idempotency_key) DO NOTHING"
            ),
            {"key": key, "draft_id": draft_id, "payload": json.dumps({"text": text})},
        )
    return result.rowcount == 1


async def deliver_pending_outbox(
    engine: AsyncEngine, notifier: SlackNotifier, *, limit: int = 50
) -> int:
    """Deliver due messages once, retaining their frozen payload for retries."""
    delivered = 0
    async with engine.begin() as connection:
        rows = (
            await connection.execute(
                sql_text(
                    "SELECT id, draft_id, channel_id, root_ts, message_kind, payload "
                    "FROM slack_outbox WHERE status IN ('pending','failed') "
                    "AND next_attempt_at <= NOW() ORDER BY id LIMIT :limit "
                    "FOR UPDATE SKIP LOCKED"
                ),
                {"limit": limit},
            )
        ).mappings()
        for row in rows:
            try:
                result = await notifier.send_message(
                    str(row["payload"]["text"]),
                    channel_id=row["channel_id"],
                    thread_ts=row["root_ts"],
                )
                if row["message_kind"] == "root_approval":
                    bound = await connection.execute(
                        sql_text(
                            "UPDATE drafts SET slack_channel_id=:channel, slack_root_ts=:root, "
                            "root_draft_id=id WHERE id=:draft_id AND revision_number=0 "
                            "AND status='queued' AND slack_channel_id IS NULL "
                            "AND slack_root_ts IS NULL"
                        ),
                        {
                            "channel": result.channel_id,
                            "root": result.ts,
                            "draft_id": row["draft_id"],
                        },
                    )
                    if bound.rowcount != 1:
                        raise RuntimeError("root draft could not be bound after Slack delivery")
                await connection.execute(
                    sql_text(
                        "UPDATE slack_outbox SET status='delivered', delivered_at=NOW(), "
                        "attempt_count=attempt_count+1, last_failure_category=NULL WHERE id=:id"
                    ),
                    {"id": row["id"]},
                )
                delivered += 1
            except Exception as error:
                await connection.execute(
                    sql_text(
                        "UPDATE slack_outbox SET status='failed', attempt_count=attempt_count+1, "
                        "next_attempt_at=NOW()+INTERVAL '1 hour', "
                        "last_failure_category=:category WHERE id=:id"
                    ),
                    {"id": row["id"], "category": type(error).__name__},
                )
                log.warning(
                    "slack_outbox.delivery_failed",
                    outbox_id=row["id"],
                    draft_id=row["draft_id"],
                    failure_category=type(error).__name__,
                )
    return delivered
