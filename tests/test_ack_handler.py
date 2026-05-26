"""Slack ack handler — DB integration."""

import asyncio
import os
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from peermarket_agent.config import get_settings
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


async def test_handle_approve_meta_draft_schedules_pipeline(monkeypatch):
    """Approving a meta_ad_creative draft schedules process_approved_meta_draft."""
    # Seed minimum env so get_settings() works inside the handler
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("SLACK_FOUNDER_USER_ID", "U0FOUNDER")
    monkeypatch.setenv("GITHUB_APP_ID", "1")
    monkeypatch.setenv(
        "GITHUB_APP_PRIVATE_KEY",
        "-----BEGIN RSA PRIVATE KEY-----\nx\n-----END RSA PRIVATE KEY-----",
    )
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "1")
    monkeypatch.setenv("PEERMARKET_PROD_DB_READONLY_URL", "postgresql+asyncpg://r:o@host/peer")
    monkeypatch.setenv("RECRAFT_API_KEY", "rk")
    monkeypatch.setenv("RESEND_API_KEY", "re")
    monkeypatch.setenv("BACKBLAZE_B2_KEY_ID", "kid")
    monkeypatch.setenv("BACKBLAZE_B2_APP_KEY", "akey")
    monkeypatch.setenv("BACKBLAZE_B2_BUCKET", "bkt")
    monkeypatch.setenv("BACKBLAZE_B2_ENDPOINT", "endpoint")
    get_settings.cache_clear()

    url = os.environ["AGENT_DB_URL"]
    eng = create_async_engine(url, future=True)
    try:
        async with eng.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
        await run_migrations(eng)
        await seed(eng)
        draft_id = await persist_draft(
            eng,
            Draft(
                action_type_name="meta_ad_creative",
                channel="meta",
                language="NL",
                copy="x",
                asset_path=None,
                generation_cost_cents=1,
                brand_score=88,
                visual_truthfulness_pass=True,
                metadata={
                    "audience_profile_key": "declutterers",
                    "headline": "h",
                    "description": "d",
                    "cta_label": "Learn More",
                    "cta_type": "LEARN_MORE",
                    "suggested_daily_budget_eur": 10,
                    "primary_text": "p",
                },
            ),
        )

        pipeline_mock = AsyncMock(return_value=None)
        monkeypatch.setattr(
            "peermarket_agent.slack_bridge.ack_handler.process_approved_meta_draft",
            pipeline_mock,
        )

        result = await handle_ack(eng, action="approve", draft_id=draft_id, decided_by="U0FOUNDER")
        assert result.success is True

        # Wait briefly so the scheduled task gets a chance to run
        for _ in range(50):
            if pipeline_mock.await_count > 0:
                break
            await asyncio.sleep(0.01)

        pipeline_mock.assert_awaited_once()
        kwargs = pipeline_mock.await_args.kwargs
        assert kwargs["draft_id"] == draft_id
        assert kwargs["engine"] is eng
    finally:
        get_settings.cache_clear()
        await eng.dispose()
