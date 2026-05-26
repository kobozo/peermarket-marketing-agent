"""Slack ack handler — DB integration."""

import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from peermarket_agent.db.migrations import run_migrations
from peermarket_agent.db.seed import seed
from peermarket_agent.drafts import Draft, persist_draft
from peermarket_agent.slack_bridge.ack_handler import handle_ack


@pytest.fixture
async def engine_with_draft():
    url = os.environ["AGENT_DB_URL"]
    eng = create_async_engine(url, future=True)
    async with eng.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await run_migrations(eng)
    await seed(eng)
    draft_id = await persist_draft(
        eng,
        Draft(
            action_type_name="tiktok_post_organic",
            channel="tiktok",
            language="NL",
            copy="x",
            asset_path=None,
            generation_cost_cents=1,
            brand_score=88,
            visual_truthfulness_pass=True,
        ),
    )
    yield eng, draft_id
    await eng.dispose()


async def test_handle_approve_updates_status_and_replies(engine_with_draft):
    engine, draft_id = engine_with_draft
    result = await handle_ack(engine, action="approve", draft_id=draft_id, decided_by="U0B5K95BRFV")
    assert result.success is True
    assert f"Approved draft #{draft_id}" in result.reply_text
    assert "tiktok_post_organic" in result.reply_text

    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT status, decided_by FROM drafts WHERE id = :id"),
                {"id": draft_id},
            )
        ).fetchone()
    assert row[0] == "approved"
    assert row[1] == "U0B5K95BRFV"


async def test_handle_reject_updates_status(engine_with_draft):
    engine, draft_id = engine_with_draft
    result = await handle_ack(engine, action="reject", draft_id=draft_id, decided_by="U0B5K95BRFV")
    assert result.success is True
    assert f"Rejected draft #{draft_id}" in result.reply_text

    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT status FROM drafts WHERE id = :id"),
                {"id": draft_id},
            )
        ).fetchone()
    assert row[0] == "rejected"


async def test_handle_ack_unknown_draft(engine_with_draft):
    engine, _ = engine_with_draft
    result = await handle_ack(engine, action="approve", draft_id=99999, decided_by="U0B5K95BRFV")
    assert result.success is False
    assert "don't have a draft #99999" in result.reply_text


async def test_handle_ack_already_decided(engine_with_draft):
    engine, draft_id = engine_with_draft
    # First approve
    await handle_ack(engine, action="approve", draft_id=draft_id, decided_by="U1")
    # Then try to reject — should not change anything
    result = await handle_ack(engine, action="reject", draft_id=draft_id, decided_by="U2")
    assert result.success is False
    assert "already approved" in result.reply_text

    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT status, decided_by FROM drafts WHERE id = :id"),
                {"id": draft_id},
            )
        ).fetchone()
    # Status stays approved, decided_by stays U1
    assert row[0] == "approved"
    assert row[1] == "U1"
