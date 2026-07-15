"""Strict inbound routing for Slack draft-thread revision feedback."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from peermarket_agent.revisions import RevisionFeedbackEvent, record_revision_feedback

RevisionReplyKind = Literal["ignored", "unknown_root", "recorded", "duplicate"]


@dataclass(frozen=True)
class RevisionReplyResult:
    kind: RevisionReplyKind
    reply_text: str | None = None


_IGNORED_SUBTYPES = {
    "bot_message",
    "message_changed",
    "message_deleted",
    "thread_broadcast",
}


async def handle_revision_reply(engine: AsyncEngine, event: dict) -> RevisionReplyResult:
    """Store a qualifying human DM thread reply without starting generation."""
    text_value = str(event.get("text") or "").strip()
    root_ts = str(event.get("thread_ts") or "").strip()
    message_ts = str(event.get("ts") or "").strip()
    channel_id = str(event.get("channel") or "").strip()
    user_id = str(event.get("user") or "").strip()
    if (
        event.get("bot_id")
        or event.get("subtype") in _IGNORED_SUBTYPES
        or event.get("channel_type") != "im"
        or not user_id
        or not text_value
        or not root_ts
        or not message_ts
        or message_ts == root_ts
        or not channel_id
    ):
        return RevisionReplyResult(kind="ignored")

    async with engine.connect() as connection:
        known_root = (
            await connection.execute(
                text(
                    "SELECT 1 FROM drafts WHERE slack_channel_id = :channel_id "
                    "AND slack_root_ts = :root_ts AND revision_number = 0"
                ),
                {"channel_id": channel_id, "root_ts": root_ts},
            )
        ).scalar_one_or_none()
    if known_root is None:
        return RevisionReplyResult(
            kind="unknown_root",
            reply_text=(
                "I couldn't match this thread to a draft approval, so no draft was changed."
            ),
        )

    event_id = str(event.get("event_id") or event.get("client_msg_id") or "").strip()
    if not event_id:
        event_id = f"message:{channel_id}:{root_ts}:{message_ts}"
    inserted = await record_revision_feedback(
        engine,
        RevisionFeedbackEvent(
            event_id=event_id,
            channel_id=channel_id,
            root_ts=root_ts,
            message_ts=message_ts,
            text=text_value,
        ),
    )
    if not inserted:
        return RevisionReplyResult(kind="duplicate")
    return RevisionReplyResult(
        kind="recorded",
        reply_text="Feedback received — I'll group nearby replies before preparing a revision.",
    )
