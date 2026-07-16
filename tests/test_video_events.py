"""Slack video-upload event parsing and TikTok thread routing tests."""

import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from peermarket_agent.db.migrations import run_migrations
from peermarket_agent.db.seed import seed
from peermarket_agent.drafts import Draft, persist_draft
from peermarket_agent.slack_bridge.video_events import (
    extract_video_upload,
    find_thread_draft,
    update_draft_thread_metadata,
)


@pytest.fixture
async def engine():
    engine = create_async_engine(os.environ["AGENT_DB_URL"], future=True)
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await run_migrations(engine)
    await seed(engine)
    yield engine
    await engine.dispose()


def test_extract_video_upload_from_thread_message():
    upload = extract_video_upload(
        {
            "channel": "C123",
            "thread_ts": "1710000000.123456",
            "ts": "1710000001.000001",
            "user": "U123",
            "files": [
                {
                    "id": "F123",
                    "name": "recording.mp4",
                    "mimetype": "video/mp4",
                    "size": 1234,
                }
            ],
        }
    )

    assert upload is not None
    assert upload.file_id == "F123"
    assert upload.thread_ts == "1710000000.123456"
    assert upload.message_ts == "1710000001.000001"
    assert upload.channel_id == "C123"
    assert upload.user_id == "U123"
    assert upload.filename == "recording.mp4"
    assert upload.mimetype == "video/mp4"
    assert upload.size_bytes == 1234


def test_extract_video_upload_uses_thread_timestamp_when_event_timestamp_is_missing():
    upload = extract_video_upload(
        {
            "channel": "C123",
            "thread_ts": "1710000000.123456",
            "user": "U123",
            "files": [{"id": "F123", "name": "recording.mp4", "mimetype": "video/mp4"}],
        }
    )

    assert upload is not None
    assert upload.message_ts == "1710000000.123456"


def test_extract_video_upload_rejects_non_thread_file_message():
    assert (
        extract_video_upload(
            {
                "channel": "C123",
                "user": "U123",
                "files": [{"id": "F123", "name": "recording.mp4", "mimetype": "video/mp4"}],
            }
        )
        is None
    )


def test_extract_video_upload_rejects_non_video_file():
    assert (
        extract_video_upload(
            {
                "channel": "C123",
                "thread_ts": "1710000000.123456",
                "user": "U123",
                "files": [{"id": "F123", "name": "notes.pdf", "mimetype": "application/pdf"}],
            }
        )
        is None
    )


def test_extract_video_upload_rejects_bot_message():
    assert (
        extract_video_upload(
            {
                "bot_id": "B123",
                "channel": "C123",
                "thread_ts": "1710000000.123456",
                "files": [{"id": "F123", "name": "recording.mp4", "mimetype": "video/mp4"}],
            }
        )
        is None
    )


async def test_update_draft_thread_metadata_merges_notifier_reference(engine):
    draft_id = await persist_draft(
        engine,
        Draft(
            action_type_name="tiktok_post_organic",
            channel="tiktok",
            language="NL",
            copy="x",
            asset_path=None,
            generation_cost_cents=0,
            brand_score=80,
            visual_truthfulness_pass=True,
            metadata={"recording_notes": "Use daylight"},
        ),
    )

    await update_draft_thread_metadata(engine, draft_id, "C123", "1710000000.123456")
    async with engine.connect() as conn:
        metadata = await conn.scalar(
            text("SELECT metadata FROM drafts WHERE id = :id"), {"id": draft_id}
        )

    assert metadata == {
        "recording_notes": "Use daylight",
        "slack_channel_id": "C123",
        "slack_ts": "1710000000.123456",
    }


async def test_find_thread_draft_returns_only_matching_tiktok_draft(engine):
    tiktok_id = await persist_draft(
        engine,
        Draft(
            action_type_name="tiktok_post_organic",
            channel="tiktok",
            language="NL",
            copy="x",
            asset_path=None,
            generation_cost_cents=0,
            brand_score=80,
            visual_truthfulness_pass=True,
            metadata={"slack_channel_id": "C123", "slack_ts": "1710000000.123456"},
        ),
    )

    assert await find_thread_draft(engine, "C123", "1710000000.123456") == tiktok_id
    assert await find_thread_draft(engine, "C999", "1710000000.123456") is None


@pytest.mark.parametrize("status", ["rejected", "published"])
async def test_find_thread_draft_excludes_terminal_tiktok_drafts(engine, status):
    draft_id = await persist_draft(
        engine,
        Draft(
            action_type_name="tiktok_post_organic",
            channel="tiktok",
            language="NL",
            copy="x",
            asset_path=None,
            generation_cost_cents=0,
            brand_score=80,
            visual_truthfulness_pass=True,
            metadata={"slack_channel_id": "C123", "slack_ts": "1710000000.123456"},
        ),
    )
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE drafts SET status = :status WHERE id = :id"),
            {"status": status, "id": draft_id},
        )

    assert await find_thread_draft(engine, "C123", "1710000000.123456") is None
