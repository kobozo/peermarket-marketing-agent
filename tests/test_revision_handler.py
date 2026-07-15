"""Routing tests for inbound Slack revision feedback."""

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from peermarket_agent.db.migrations import run_migrations
from peermarket_agent.db.seed import seed
from peermarket_agent.drafts import Draft, persist_draft
from peermarket_agent.revisions import bind_draft_thread, claim_feedback_batch
from peermarket_agent.slack_bridge.revision_handler import handle_revision_reply


@pytest.fixture
async def engine():
    eng = create_async_engine(os.environ["AGENT_DB_URL"], future=True)
    async with eng.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await run_migrations(eng)
    await seed(eng)
    yield eng
    await eng.dispose()


async def _bound_draft(engine) -> int:
    draft_id = await persist_draft(
        engine,
        Draft(
            action_type_name="tiktok_post_organic",
            channel="tiktok",
            language="NL",
            copy="Original",
            asset_path=None,
            generation_cost_cents=1,
            brand_score=90,
            visual_truthfulness_pass=True,
            metadata={},
        ),
    )
    await bind_draft_thread(engine, draft_id, "D123", "100.000")
    return draft_id


def _event(**overrides):
    event = {
        "event_id": "Ev1",
        "channel": "D123",
        "channel_type": "im",
        "thread_ts": "100.000",
        "ts": "100.001",
        "user": "U123",
        "text": "Make it shorter",
    }
    event.update(overrides)
    return event


async def test_known_human_dm_thread_reply_is_stored_once(engine):
    await _bound_draft(engine)

    first = await handle_revision_reply(engine, _event())
    duplicate_event = await handle_revision_reply(engine, _event())
    duplicate_message = await handle_revision_reply(engine, _event(event_id="Ev2"))

    assert first.kind == "recorded"
    assert first.reply_text
    assert duplicate_event.kind == "duplicate"
    assert duplicate_message.kind == "duplicate"
    async with engine.connect() as conn:
        assert (
            await conn.execute(text("SELECT count(*) FROM draft_revision_feedback"))
        ).scalar_one() == 1


async def test_unknown_root_explains_that_nothing_changed(engine):
    result = await handle_revision_reply(engine, _event())

    assert result.kind == "unknown_root"
    assert "no draft was changed" in result.reply_text.lower()
    async with engine.connect() as conn:
        assert (
            await conn.execute(text("SELECT count(*) FROM draft_revision_feedback"))
        ).scalar_one() == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"bot_id": "B1"},
        {"subtype": "bot_message"},
        {"subtype": "message_changed"},
        {"subtype": "message_deleted"},
        {"subtype": "thread_broadcast"},
        {"text": "", "files": [{"id": "F1"}]},
        {"text": "   "},
        {"channel_type": "channel"},
        {"thread_ts": ""},
        {"user": ""},
    ],
)
async def test_non_revision_events_are_ignored(engine, overrides):
    await _bound_draft(engine)
    result = await handle_revision_reply(engine, _event(**overrides))
    assert result.kind == "ignored"


async def test_feedback_is_not_claimable_until_15_second_debounce_expires(engine):
    await _bound_draft(engine)
    await handle_revision_reply(engine, _event())

    assert await claim_feedback_batch(engine, "100.000") is None
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE draft_revision_feedback SET received_at = :received_at "
                "WHERE event_id = 'Ev1'"
            ),
            {"received_at": datetime.now(UTC) - timedelta(seconds=16)},
        )

    batch = await claim_feedback_batch(engine, "100.000")
    assert batch is not None
    assert batch.instructions == ("Make it shorter",)
