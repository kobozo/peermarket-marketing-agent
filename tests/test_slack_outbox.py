"""Transactional Slack approval outbox tests."""

import os
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from peermarket_agent.db.migrations import run_migrations
from peermarket_agent.db.seed import seed
from peermarket_agent.drafts import Draft, persist_draft
from peermarket_agent.revisions import bind_draft_thread
from peermarket_agent.slack_notifier import SlackMessageResult
from peermarket_agent.slack_outbox import (
    _claim_pending_outbox,
    _finalize_success,
    deliver_pending_outbox,
    enqueue_root_approval,
    enqueue_thread_approval,
)


@pytest.fixture
async def prepared_db():
    engine = create_async_engine(os.environ["AGENT_DB_URL"], future=True)
    async with engine.begin() as connection:
        await connection.execute(text("DROP SCHEMA public CASCADE"))
        await connection.execute(text("CREATE SCHEMA public"))
    await run_migrations(engine)
    await seed(engine)
    yield engine
    await engine.dispose()


async def _draft(engine, copy: str = "Original copy") -> int:
    return await persist_draft(
        engine,
        Draft(
            action_type_name="tiktok_post_organic",
            channel="tiktok",
            language="NL",
            copy=copy,
            asset_path=None,
            generation_cost_cents=1,
            brand_score=90,
            visual_truthfulness_pass=True,
            metadata={},
        ),
    )


async def test_root_delivery_binds_only_after_success_and_is_idempotent(prepared_db):
    draft_id = await _draft(prepared_db)
    assert await enqueue_root_approval(prepared_db, draft_id=draft_id, text="frozen root")
    assert not await enqueue_root_approval(
        prepared_db, draft_id=draft_id, text="must not replace payload"
    )
    notifier = AsyncMock()
    notifier.send_message = AsyncMock(
        side_effect=[RuntimeError("slack unavailable"), SlackMessageResult("D1", "100.01")]
    )

    assert await deliver_pending_outbox(prepared_db, notifier) == 0
    async with prepared_db.connect() as connection:
        row = (
            await connection.execute(
                text("SELECT slack_root_ts FROM drafts WHERE id=:id"), {"id": draft_id}
            )
        ).one()
        payload = (
            await connection.execute(text("SELECT payload->>'text' FROM slack_outbox"))
        ).scalar_one()
    assert row[0] is None
    assert payload == "frozen root"

    async with prepared_db.begin() as connection:
        await connection.execute(text("UPDATE slack_outbox SET next_attempt_at=NOW()"))
    assert await deliver_pending_outbox(prepared_db, notifier) == 1
    assert await deliver_pending_outbox(prepared_db, notifier) == 0
    assert notifier.send_message.await_count == 2
    async with prepared_db.connect() as connection:
        row = (
            await connection.execute(
                text("SELECT slack_channel_id, slack_root_ts FROM drafts WHERE id=:id"),
                {"id": draft_id},
            )
        ).one()
    assert row == ("D1", "100.01")


async def test_thread_delivery_uses_stored_channel_and_root(prepared_db):
    root_id = await _draft(prepared_db)
    await bind_draft_thread(prepared_db, root_id, "D9", "200.02")
    async with prepared_db.begin() as connection:
        draft_id = (
            await connection.execute(
                text(
                    "INSERT INTO drafts (action_type_id, channel, language, copy, "
                    "generation_cost_cents, brand_score, visual_truthfulness_pass, metadata, "
                    "parent_draft_id, root_draft_id, revision_number, revision_feedback, "
                    "slack_channel_id, slack_root_ts) SELECT action_type_id, channel, language, "
                    "'Revised complete copy', generation_cost_cents, brand_score, "
                    "visual_truthfulness_pass, metadata, id, id, 1, 'warmer', "
                    "slack_channel_id, slack_root_ts FROM drafts WHERE id=:root RETURNING id"
                ),
                {"root": root_id},
            )
        ).scalar_one()
    assert await enqueue_thread_approval(
        prepared_db,
        draft_id=draft_id,
        text="frozen revision",
        idempotency_key="revision:7",
    )
    notifier = AsyncMock()
    notifier.send_message = AsyncMock(return_value=SlackMessageResult("D9", "201.03"))

    assert await deliver_pending_outbox(prepared_db, notifier) == 1

    notifier.send_message.assert_awaited_once_with(
        "frozen revision", channel_id="D9", thread_ts="200.02"
    )


async def test_concurrent_workers_claim_each_message_once(prepared_db):
    ids = [await _draft(prepared_db, f"copy {index}") for index in range(2)]
    for draft_id in ids:
        await enqueue_root_approval(prepared_db, draft_id=draft_id, text=f"root {draft_id}")

    first, second = await __import__("asyncio").gather(
        _claim_pending_outbox(prepared_db, owner="worker-a", limit=2),
        _claim_pending_outbox(prepared_db, owner="worker-b", limit=2),
    )

    assert {item.id for item in first}.isdisjoint({item.id for item in second})
    assert len(first) + len(second) == 2


async def test_expired_lease_is_reclaimed_and_stale_owner_cannot_finalize(prepared_db):
    draft_id = await _draft(prepared_db)
    await enqueue_root_approval(prepared_db, draft_id=draft_id, text="root")
    claimed = await _claim_pending_outbox(prepared_db, owner="old", limit=1)
    async with prepared_db.begin() as connection:
        await connection.execute(
            text("UPDATE slack_outbox SET lease_expires_at=:expired"),
            {"expired": datetime.now(UTC) - timedelta(seconds=1)},
        )
    reclaimed = await _claim_pending_outbox(prepared_db, owner="new", limit=1)

    assert reclaimed[0].id == claimed[0].id
    assert not await _finalize_success(
        prepared_db,
        claimed[0],
        owner="old",
        result=SlackMessageResult("D-old", "1.0"),
    )
    assert await _finalize_success(
        prepared_db,
        reclaimed[0],
        owner="new",
        result=SlackMessageResult("D-new", "2.0"),
    )


async def test_successful_first_message_survives_second_failure(prepared_db):
    first_id = await _draft(prepared_db, "first")
    second_id = await _draft(prepared_db, "second")
    for draft_id in (first_id, second_id):
        await enqueue_root_approval(prepared_db, draft_id=draft_id, text=str(draft_id))
    notifier = AsyncMock()
    notifier.send_message = AsyncMock(
        side_effect=[SlackMessageResult("D1", "1.0"), RuntimeError("down")]
    )

    assert await deliver_pending_outbox(prepared_db, notifier) == 1

    async with prepared_db.connect() as connection:
        statuses = (
            (await connection.execute(text("SELECT status FROM slack_outbox ORDER BY id")))
            .scalars()
            .all()
        )
    assert statuses == ["delivered", "failed"]


async def test_cancellation_between_rows_preserves_finalized_first(prepared_db):
    for value in ("first", "second"):
        draft_id = await _draft(prepared_db, value)
        await enqueue_root_approval(prepared_db, draft_id=draft_id, text=value)
    notifier = AsyncMock()
    notifier.send_message = AsyncMock(
        side_effect=[SlackMessageResult("D1", "1.0"), __import__("asyncio").CancelledError()]
    )

    with pytest.raises(__import__("asyncio").CancelledError):
        await deliver_pending_outbox(prepared_db, notifier)

    async with prepared_db.connect() as connection:
        statuses = (
            (await connection.execute(text("SELECT status FROM slack_outbox ORDER BY id")))
            .scalars()
            .all()
        )
    assert statuses == ["delivered", "pending"]
