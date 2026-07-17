"""Durable Slack approval-message outbox."""

import json
import uuid
from dataclasses import dataclass

import structlog
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncEngine

from peermarket_agent.slack_notifier import SlackNotifier

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class OutboxMessage:
    id: int
    draft_id: int
    channel_id: str | None
    root_ts: str | None
    message_kind: str
    text: str


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
        if result.rowcount == 1:
            await connection.execute(
                sql_text(
                    "UPDATE drafts SET root_draft_id=id WHERE id=:draft_id "
                    "AND revision_number=0 AND root_draft_id IS NULL"
                ),
                {"draft_id": draft_id},
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
    engine: AsyncEngine,
    notifier: SlackNotifier,
    *,
    limit: int = 50,
    lease_seconds: int = 300,
) -> int:
    """Deliver due messages once, retaining their frozen payload for retries."""
    owner = str(uuid.uuid4())
    delivered = 0
    for _ in range(limit):
        rows = await _claim_pending_outbox(engine, owner=owner, lease_seconds=lease_seconds)
        if not rows:
            break
        row = rows[0]
        if not await _lease_is_current(engine, row, owner=owner):
            continue
        try:
            result = await notifier.send_message(
                row.text,
                channel_id=row.channel_id,
                thread_ts=row.root_ts,
            )
            if await _finalize_success(engine, row, owner=owner, result=result):
                delivered += 1
        except Exception as error:
            await _finalize_failure(engine, row, owner=owner, failure_category=type(error).__name__)
            log.warning(
                "slack_outbox.delivery_failed",
                outbox_id=row.id,
                draft_id=row.draft_id,
                failure_category=type(error).__name__,
            )
    return delivered


async def _lease_is_current(engine: AsyncEngine, message: OutboxMessage, *, owner: str) -> bool:
    """Atomically cancel stale approvals or verify ownership at the send boundary."""
    async with engine.begin() as connection:
        await connection.execute(
            sql_text(
                "UPDATE slack_outbox o SET status='obsolete', lease_owner=NULL, "
                "lease_expires_at=NULL, last_failure_category='draft_not_eligible' "
                "WHERE o.id=:id AND o.lease_owner=:owner AND o.status IN ('pending','failed') "
                "AND ((o.message_kind='autonomy_audit' AND EXISTS (SELECT 1 FROM slack_outbox newer "
                "WHERE newer.autonomy_campaign_id=o.autonomy_campaign_id "
                "AND newer.message_kind='autonomy_audit' "
                "AND newer.id>o.id)) OR (o.message_kind<>'autonomy_audit' AND NOT EXISTS "
                "(SELECT 1 FROM drafts d WHERE d.id=o.draft_id AND d.status='queued' "
                "AND NOT EXISTS (SELECT 1 FROM drafts newer WHERE newer.root_draft_id=d.root_draft_id "
                "AND newer.revision_number>d.revision_number))))"
            ),
            {"id": message.id, "owner": owner},
        )
        current = (
            await connection.execute(
                sql_text(
                    "SELECT 1 FROM slack_outbox WHERE id=:id AND lease_owner=:owner "
                    "AND lease_expires_at > NOW() AND status IN ('pending','failed')"
                ),
                {"id": message.id, "owner": owner},
            )
        ).scalar_one_or_none()
    return current is not None


async def _claim_pending_outbox(
    engine: AsyncEngine, *, owner: str, lease_seconds: int = 300
) -> tuple[OutboxMessage, ...]:
    """Lease at most one due row without holding locks during I/O."""
    async with engine.begin() as connection:
        rows = (
            (
                await connection.execute(
                    sql_text(
                        "WITH candidates AS (SELECT id FROM slack_outbox "
                        "WHERE status IN ('pending','failed') AND next_attempt_at <= NOW() "
                        "AND (lease_expires_at IS NULL OR lease_expires_at <= NOW()) "
                        "ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED) "
                        "UPDATE slack_outbox AS outbox SET lease_owner=:owner, "
                        "lease_expires_at=NOW()+make_interval(secs => :lease_seconds), "
                        "attempt_count=attempt_count+1 FROM candidates "
                        "WHERE outbox.id=candidates.id RETURNING outbox.id, outbox.draft_id, "
                        "outbox.channel_id, outbox.root_ts, outbox.message_kind, outbox.payload"
                    ),
                    {"owner": owner, "lease_seconds": lease_seconds},
                )
            )
            .mappings()
            .all()
        )
    return tuple(
        OutboxMessage(
            id=int(row["id"]),
            draft_id=int(row["draft_id"]),
            channel_id=row["channel_id"],
            root_ts=row["root_ts"],
            message_kind=str(row["message_kind"]),
            text=str(row["payload"]["text"]),
        )
        for row in rows
    )


async def _finalize_success(engine, message, *, owner: str, result) -> bool:
    """Commit one success only when this worker still owns the lease."""
    async with engine.begin() as connection:
        owned = (
            await connection.execute(
                sql_text(
                    "SELECT id FROM slack_outbox WHERE id=:id AND lease_owner=:owner "
                    "AND status IN ('pending','failed') FOR UPDATE"
                ),
                {"id": message.id, "owner": owner},
            )
        ).scalar_one_or_none()
        if owned is None:
            return False
        if message.message_kind == "root_approval":
            bound = await connection.execute(
                sql_text(
                    "UPDATE drafts SET slack_channel_id=:channel, slack_root_ts=:root, "
                    "root_draft_id=id WHERE id=:draft_id AND revision_number=0 AND ("
                    "(slack_channel_id IS NULL AND slack_root_ts IS NULL) OR "
                    "(slack_channel_id=:channel AND slack_root_ts=:root))"
                ),
                {
                    "channel": result.channel_id,
                    "root": result.ts,
                    "draft_id": message.draft_id,
                },
            )
            if bound.rowcount != 1:
                raise RuntimeError("root draft could not be bound after Slack delivery")
        await connection.execute(
            sql_text(
                "UPDATE slack_outbox SET status='delivered', delivered_at=NOW(), "
                "last_failure_category=NULL, lease_owner=NULL, lease_expires_at=NULL "
                "WHERE id=:id AND lease_owner=:owner"
            ),
            {"id": message.id, "owner": owner},
        )
    return True


async def _finalize_failure(
    engine: AsyncEngine, message: OutboxMessage, *, owner: str, failure_category: str
) -> bool:
    async with engine.begin() as connection:
        result = await connection.execute(
            sql_text(
                "UPDATE slack_outbox SET status='failed', "
                "next_attempt_at=NOW()+INTERVAL '1 hour', last_failure_category=:category, "
                "lease_owner=NULL, lease_expires_at=NULL WHERE id=:id AND lease_owner=:owner "
                "AND status IN ('pending','failed')"
            ),
            {"id": message.id, "owner": owner, "category": failure_category},
        )
    return result.rowcount == 1
