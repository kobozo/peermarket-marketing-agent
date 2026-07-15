"""Slack draft revision repository tests."""

import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from peermarket_agent.db.migrations import run_migrations
from peermarket_agent.db.seed import seed
from peermarket_agent.drafts import Draft, persist_draft
from peermarket_agent.revisions import (
    RevisionFeedbackEvent,
    bind_draft_thread,
    claim_feedback_batch,
    persist_revision_and_supersede,
    record_revision_feedback,
)


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


def draft(copy: str = "original") -> Draft:
    return Draft(
        action_type_name="tiktok_post_organic",
        channel="tiktok",
        language="NL",
        copy=copy,
        asset_path=None,
        generation_cost_cents=1,
        brand_score=90,
        visual_truthfulness_pass=True,
        metadata={"unchanged": True},
    )


def event(event_id: str, message_ts: str, text_value: str) -> RevisionFeedbackEvent:
    return RevisionFeedbackEvent(
        event_id=event_id,
        channel_id="D123",
        root_ts="100.000",
        message_ts=message_ts,
        text=text_value,
    )


def eligible_now() -> datetime:
    return datetime.now(UTC) + timedelta(seconds=16)


async def test_bind_draft_thread_is_idempotent_and_root_is_unique(engine):
    first = await persist_draft(engine, draft())
    second = await persist_draft(engine, draft("other"))

    await bind_draft_thread(engine, first, "D123", "100.000")
    await bind_draft_thread(engine, first, "D123", "100.000")
    with pytest.raises(ValueError, match="already bound"):
        await bind_draft_thread(engine, second, "D123", "100.000")


async def test_duplicate_slack_feedback_delivery_is_ignored(engine):
    original = await persist_draft(engine, draft())
    await bind_draft_thread(engine, original, "D123", "100.000")

    assert await record_revision_feedback(engine, event("Ev1", "100.002", "Shorter"))
    assert not await record_revision_feedback(engine, event("Ev1", "100.002", "Shorter"))
    assert not await record_revision_feedback(engine, event("Ev2", "100.002", "Shorter"))


async def test_claim_feedback_batch_is_ordered_and_claimed_once(engine):
    original = await persist_draft(engine, draft())
    await bind_draft_thread(engine, original, "D123", "100.000")
    await record_revision_feedback(engine, event("Ev2", "100.003", "Second"))
    await record_revision_feedback(engine, event("Ev1", "100.002", "First"))

    batch = await claim_feedback_batch(engine, "D123", "100.000", now=eligible_now())
    assert batch is not None
    assert batch.root_draft_id == original
    assert batch.feedback_ids
    assert batch.instructions == ("First", "Second")
    assert await claim_feedback_batch(engine, "D123", "100.000", now=eligible_now()) is None


async def test_concurrent_feedback_claims_have_one_winner(engine):
    original = await persist_draft(engine, draft())
    await bind_draft_thread(engine, original, "D123", "100.000")
    await record_revision_feedback(engine, event("Ev1", "100.002", "Shorter"))

    results = await asyncio.gather(
        claim_feedback_batch(engine, "D123", "100.000", now=eligible_now()),
        claim_feedback_batch(engine, "D123", "100.000", now=eligible_now()),
    )
    assert sum(result is not None for result in results) == 1


async def test_revision_insert_and_supersede_are_atomic(engine):
    original = await persist_draft(engine, draft())
    await bind_draft_thread(engine, original, "D123", "100.000")
    await record_revision_feedback(engine, event("Ev1", "100.002", "Shorter"))
    batch = await claim_feedback_batch(engine, "D123", "100.000", now=eligible_now())
    assert batch is not None

    revised_id = await persist_revision_and_supersede(
        engine, original, draft("revised"), batch.feedback_ids
    )
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT id, status, parent_draft_id, root_draft_id, revision_number, "
                    "slack_channel_id, slack_root_ts FROM drafts ORDER BY id"
                )
            )
        ).fetchall()
        feedback_status = (
            await conn.execute(text("SELECT status FROM draft_revision_feedback"))
        ).scalar_one()

    assert rows == [
        (original, "superseded", None, original, 0, "D123", "100.000"),
        (revised_id, "queued", original, original, 1, "D123", "100.000"),
    ]
    assert feedback_status == "applied"


async def test_invalid_predecessor_rolls_back_feedback_and_draft(engine):
    original = await persist_draft(engine, draft())
    await bind_draft_thread(engine, original, "D123", "100.000")
    await record_revision_feedback(engine, event("Ev1", "100.002", "Shorter"))
    batch = await claim_feedback_batch(engine, "D123", "100.000", now=eligible_now())
    assert batch is not None

    with pytest.raises(ValueError, match="latest queued predecessor"):
        await persist_revision_and_supersede(
            engine, original + 999, draft("bad"), batch.feedback_ids
        )

    async with engine.connect() as conn:
        count = (await conn.execute(text("SELECT count(*) FROM drafts"))).scalar_one()
        status = (
            await conn.execute(text("SELECT status FROM draft_revision_feedback"))
        ).scalar_one()
    assert count == 1
    assert status == "processing"


async def test_stale_predecessor_cannot_create_another_latest_revision(engine):
    original = await persist_draft(engine, draft())
    await bind_draft_thread(engine, original, "D123", "100.000")
    await record_revision_feedback(engine, event("Ev1", "100.002", "Shorter"))
    first_batch = await claim_feedback_batch(engine, "D123", "100.000", now=eligible_now())
    assert first_batch is not None
    await persist_revision_and_supersede(
        engine, original, draft("revision 1"), first_batch.feedback_ids
    )

    await record_revision_feedback(engine, event("Ev2", "100.003", "Punchier"))
    second_batch = await claim_feedback_batch(engine, "D123", "100.000", now=eligible_now())
    assert second_batch is not None
    with pytest.raises(ValueError, match="latest queued predecessor"):
        await persist_revision_and_supersede(
            engine, original, draft("wrong branch"), second_batch.feedback_ids
        )
