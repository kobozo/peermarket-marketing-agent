"""Slack ack handler — translates parsed acks into DB updates + reply text."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from peermarket_agent.config import get_settings
from peermarket_agent.meta_pipeline import process_approved_meta_draft
from peermarket_agent.slack_bridge.ack_parser import AckAction
from peermarket_agent.slack_notifier import SlackNotifier

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class AckResult:
    success: bool
    reply_text: str


async def handle_ack(
    engine: AsyncEngine,
    *,
    action: AckAction,
    draft_id: int,
    decided_by: str,
) -> AckResult:
    """Update the draft row and return a reply message for Slack."""
    new_status = "approved" if action == "approve" else "rejected"
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT d.status, at.name, d.root_draft_id "
                    "FROM drafts d JOIN action_types at ON at.id = d.action_type_id "
                    "WHERE d.id = :id FOR UPDATE OF d"
                ),
                {"id": draft_id},
            )
        ).fetchone()
        if row is None:
            return AckResult(
                success=False,
                reply_text=(f"⚠️ I don't have a draft #{draft_id} — maybe it was already decided?"),
            )
        current_status, action_type_name, root_draft_id = row
        latest_id = draft_id
        if root_draft_id is not None:
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtext('draft_ack'), hashint8(:root))"),
                {"root": root_draft_id},
            )
            latest_id = int(
                (
                    await conn.execute(
                        text(
                            "SELECT id FROM drafts WHERE root_draft_id=:root "
                            "ORDER BY revision_number DESC LIMIT 1"
                        ),
                        {"root": root_draft_id},
                    )
                ).scalar_one()
            )
            if latest_id != draft_id:
                return AckResult(
                    success=False,
                    reply_text=(
                        f"⚠️ Draft #{draft_id} is not the latest variant. "
                        f"No change — decide draft #{latest_id} instead."
                    ),
                )
        if current_status != "queued":
            return AckResult(
                success=False,
                reply_text=(f"⚠️ Draft #{draft_id} was already {current_status}. No change."),
            )
        updated = await conn.execute(
            text(
                "UPDATE drafts SET status = :new_status, "
                "decided_at = NOW(), decided_by = :by "
                "WHERE id = :id AND status = 'queued' "
                "AND (root_draft_id IS NULL OR NOT EXISTS ("
                "SELECT 1 FROM drafts newer WHERE newer.root_draft_id=drafts.root_draft_id "
                "AND newer.revision_number > drafts.revision_number))"
            ),
            {"new_status": new_status, "by": decided_by, "id": draft_id},
        )
        if updated.rowcount != 1:
            return AckResult(
                success=False,
                reply_text=f"⚠️ Draft #{draft_id} was decided concurrently. No change.",
            )
    log.info(
        "slack_ack.applied",
        draft_id=draft_id,
        action=action,
        by=decided_by,
        action_type=action_type_name,
    )
    if action == "approve" and action_type_name == "meta_ad_creative":
        settings = get_settings()
        notifier = SlackNotifier(
            bot_token=settings.slack_bot_token,
            founder_user_id=settings.slack_founder_user_id,
        )
        asyncio.create_task(
            process_approved_meta_draft(
                engine=engine,
                draft_id=draft_id,
                settings=settings,
                notifier=notifier,
            )
        )
    if action == "approve":
        reply = f"✅ Approved draft #{draft_id} ({action_type_name})."
    else:
        reply = f"❌ Rejected draft #{draft_id} ({action_type_name}). Next time I'll try harder."
    return AckResult(success=True, reply_text=reply)
