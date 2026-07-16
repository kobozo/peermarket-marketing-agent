"""Slack thread video intake and draft-thread lookup helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


@dataclass(frozen=True)
class VideoUpload:
    """A supported video file attached to a Slack draft thread."""

    file_id: str
    thread_ts: str
    channel_id: str
    user_id: str
    filename: str
    mimetype: str
    size_bytes: int
    message_ts: str = ""


_VIDEO_MIMETYPES = {
    ".mp4": {"video/mp4"},
    ".mov": {"video/quicktime"},
    ".webm": {"video/webm"},
}


def extract_video_upload(event: dict) -> VideoUpload | None:
    """Return the first supported video from a human Slack thread reply."""
    if event.get("bot_id") or event.get("subtype") == "bot_message":
        return None

    thread_ts = event.get("thread_ts")
    channel_id = event.get("channel")
    user_id = event.get("user")
    if not all(isinstance(value, str) and value for value in (thread_ts, channel_id, user_id)):
        return None

    for file_info in event.get("files") or []:
        filename = file_info.get("name")
        mimetype = file_info.get("mimetype")
        file_id = file_info.get("id")
        if not all(isinstance(value, str) and value for value in (filename, mimetype, file_id)):
            continue
        extension = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if mimetype.lower() not in _VIDEO_MIMETYPES.get(extension, set()):
            continue
        size = file_info.get("size", 0)
        return VideoUpload(
            file_id=file_id,
            thread_ts=thread_ts,
            channel_id=channel_id,
            user_id=user_id,
            filename=filename,
            mimetype=mimetype,
            size_bytes=size if isinstance(size, int) else 0,
            message_ts=event.get("ts")
            if isinstance(event.get("ts"), str) and event["ts"]
            else thread_ts,
        )
    return None


async def find_thread_draft(engine: AsyncEngine, channel_id: str, thread_ts: str) -> int | None:
    """Resolve a Slack root message to its persisted TikTok draft, if any."""
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT d.id FROM drafts d "
                    "JOIN action_types at ON at.id = d.action_type_id "
                    "WHERE at.name = 'tiktok_post_organic' "
                    "AND d.channel = 'tiktok' "
                    "AND d.status IN ('queued', 'approved') "
                    "AND ((d.metadata ->> 'slack_channel_id' = :channel_id "
                    "AND d.metadata ->> 'slack_ts' = :thread_ts) OR "
                    "(d.slack_channel_id = :channel_id AND d.slack_root_ts = :thread_ts))"
                ),
                {"channel_id": channel_id, "thread_ts": thread_ts},
            )
        ).fetchone()
    return int(row[0]) if row is not None else None


async def update_draft_thread_metadata(
    engine: AsyncEngine, draft_id: int, channel_id: str, message_ts: str
) -> None:
    """Merge a Slack root-message reference into a draft's JSONB metadata."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE drafts SET metadata = COALESCE(metadata, '{}'::jsonb) "
                "|| CAST(:metadata AS JSONB) WHERE id = :draft_id"
            ),
            {
                "draft_id": draft_id,
                "metadata": json.dumps({"slack_channel_id": channel_id, "slack_ts": message_ts}),
            },
        )
