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
    list_ready_feedback_threads,
    mark_feedback_failed,
    persist_revision_and_supersede,
    record_revision_feedback,
    renew_generation_lease,
)
from peermarket_agent.slack_outbox import enqueue_root_approval


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


async def test_root_lease_blocks_other_owner_and_stale_processing_is_reclaimed(engine):
    original = await persist_draft(engine, draft())
    await bind_draft_thread(engine, original, "D123", "100.000")
    await record_revision_feedback(engine, event("Ev1", "100.002", "Shorter"))
    now = eligible_now()

    first = await claim_feedback_batch(
        engine, "D123", "100.000", now=now, owner="worker-a", lease_seconds=30
    )
    blocked = await claim_feedback_batch(
        engine,
        "D123",
        "100.000",
        now=now + timedelta(seconds=10),
        owner="worker-b",
        lease_seconds=30,
    )
    reclaimed = await claim_feedback_batch(
        engine,
        "D123",
        "100.000",
        now=now + timedelta(seconds=31),
        owner="worker-b",
        lease_seconds=30,
    )

    assert first is not None and first.lease_owner == "worker-a"
    assert blocked is None
    assert reclaimed is not None and reclaimed.feedback_ids == first.feedback_ids
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT processing_owner, processing_attempts FROM draft_revision_feedback")
            )
        ).one()
    assert row == ("worker-b", 2)


async def test_only_owner_can_renew_root_generation_lease(engine):
    original = await persist_draft(engine, draft())
    await bind_draft_thread(engine, original, "D123", "100.000")
    await record_revision_feedback(engine, event("Ev1", "100.002", "Shorter"))
    now = eligible_now()
    batch = await claim_feedback_batch(
        engine, "D123", "100.000", now=now, owner="worker-a", lease_seconds=30
    )
    assert batch is not None

    assert not await renew_generation_lease(
        engine, original, "worker-b", now=now + timedelta(seconds=20), lease_seconds=30
    )
    assert await renew_generation_lease(
        engine, original, "worker-a", now=now + timedelta(seconds=20), lease_seconds=30
    )
    assert (
        await claim_feedback_batch(
            engine,
            "D123",
            "100.000",
            now=now + timedelta(seconds=31),
            owner="worker-b",
            lease_seconds=30,
        )
        is None
    )


async def test_stale_processing_batch_is_discoverable_on_startup(engine):
    original = await persist_draft(engine, draft())
    await bind_draft_thread(engine, original, "D123", "100.000")
    await record_revision_feedback(engine, event("Ev1", "100.002", "Shorter"))
    batch = await claim_feedback_batch(
        engine, "D123", "100.000", now=eligible_now(), owner="dead-worker"
    )
    assert batch is not None
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE draft_revision_feedback SET processing_lease_expires_at="
                "NOW()-INTERVAL '1 second'"
            )
        )
        await conn.execute(
            text(
                "UPDATE draft_revision_generation_leases SET lease_expires_at="
                "NOW()-INTERVAL '1 second'"
            )
        )

    assert await list_ready_feedback_threads(engine) == (("D123", "100.000"),)


async def test_stale_owner_cannot_fail_reclaimed_feedback(engine):
    original = await persist_draft(engine, draft())
    await bind_draft_thread(engine, original, "D123", "100.000")
    await record_revision_feedback(engine, event("Ev1", "100.002", "Shorter"))
    now = eligible_now()
    first = await claim_feedback_batch(
        engine, "D123", "100.000", now=now, owner="worker-a", lease_seconds=30
    )
    second = await claim_feedback_batch(
        engine,
        "D123",
        "100.000",
        now=now + timedelta(seconds=31),
        owner="worker-b",
        lease_seconds=30,
    )
    assert first is not None and second is not None

    with pytest.raises(RuntimeError, match="changed concurrently"):
        await mark_feedback_failed(
            engine, first.feedback_ids, "late_failure", lease_owner=first.lease_owner
        )
    async with engine.connect() as conn:
        row = (
            await conn.execute(text("SELECT status, processing_owner FROM draft_revision_feedback"))
        ).one()
    assert row == ("processing", "worker-b")


async def test_revision_insert_and_supersede_are_atomic(engine):
    original = await persist_draft(engine, draft())
    await bind_draft_thread(engine, original, "D123", "100.000")
    await record_revision_feedback(engine, event("Ev1", "100.002", "Shorter"))
    batch = await claim_feedback_batch(engine, "D123", "100.000", now=eligible_now())
    assert batch is not None

    revised_id = await persist_revision_and_supersede(
        engine,
        original,
        draft("revised"),
        batch.feedback_ids,
        outbox_text="Revision {{draft_id}}",
        outbox_idempotency_key="feedback:1",
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
        outbox = (
            await conn.execute(text("SELECT draft_id, payload->>'text' FROM slack_outbox"))
        ).one()

    assert rows == [
        (original, "superseded", None, original, 0, "D123", "100.000"),
        (revised_id, "queued", original, original, 1, "D123", "100.000"),
    ]
    assert feedback_status == "applied"
    assert outbox == (revised_id, f"Revision {revised_id}")


async def test_outbox_insert_failure_rolls_back_revision_supersede_and_feedback(engine):
    original = await persist_draft(engine, draft())
    await bind_draft_thread(engine, original, "D123", "100.000")
    await record_revision_feedback(engine, event("Ev1", "100.002", "Shorter"))
    batch = await claim_feedback_batch(engine, "D123", "100.000", now=eligible_now())
    assert batch is not None
    assert await enqueue_root_approval(
        engine, draft_id=original, text="existing", idempotency_key="feedback:1"
    )

    with pytest.raises(ValueError, match="idempotency conflict"):
        await persist_revision_and_supersede(
            engine,
            original,
            draft("revised"),
            batch.feedback_ids,
            outbox_text="Revision {{draft_id}}",
            outbox_idempotency_key="feedback:1",
        )

    async with engine.connect() as conn:
        drafts = (await conn.execute(text("SELECT status FROM drafts"))).scalars().all()
        feedback = (
            await conn.execute(text("SELECT status FROM draft_revision_feedback"))
        ).scalar_one()
        outbox = (await conn.execute(text("SELECT count(*) FROM slack_outbox"))).scalar_one()
    assert drafts == ["queued"]
    assert feedback == "processing"
    assert outbox == 1


async def test_invalid_predecessor_rolls_back_feedback_and_draft(engine):
    original = await persist_draft(engine, draft())
    await bind_draft_thread(engine, original, "D123", "100.000")
    await record_revision_feedback(engine, event("Ev1", "100.002", "Shorter"))
    batch = await claim_feedback_batch(engine, "D123", "100.000", now=eligible_now())
    assert batch is not None

    with pytest.raises(ValueError, match="latest queued predecessor"):
        await persist_revision_and_supersede(
            engine,
            original + 999,
            draft("bad"),
            batch.feedback_ids,
            outbox_text="bad {{draft_id}}",
            outbox_idempotency_key="invalid",
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
        engine,
        original,
        draft("revision 1"),
        first_batch.feedback_ids,
        outbox_text="first {{draft_id}}",
        outbox_idempotency_key="first",
    )

    await record_revision_feedback(engine, event("Ev2", "100.003", "Punchier"))
    second_batch = await claim_feedback_batch(engine, "D123", "100.000", now=eligible_now())
    assert second_batch is not None
    with pytest.raises(ValueError, match="latest queued predecessor"):
        await persist_revision_and_supersede(
            engine,
            original,
            draft("wrong branch"),
            second_batch.feedback_ids,
            outbox_text="second {{draft_id}}",
            outbox_idempotency_key="second",
        )
