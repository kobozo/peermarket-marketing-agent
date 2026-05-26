"""CLI draft command tests — end-to-end orchestration."""

import os
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from peermarket_agent.agent.cli_draft import run_draft_command
from peermarket_agent.claude import ClaudeResponse
from peermarket_agent.db.migrations import run_migrations
from peermarket_agent.db.seed import seed
from peermarket_agent.prompts.brand_voice import sync_to_db


@pytest.fixture
async def prepared_db():
    url = os.environ["AGENT_DB_URL"]
    eng = create_async_engine(url, future=True)
    async with eng.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await run_migrations(eng)
    await seed(eng)
    await sync_to_db(eng)
    yield eng
    await eng.dispose()


async def test_run_draft_tiktok_persists_high_score_draft(prepared_db):
    fake_claude = AsyncMock()
    fake_claude.complete = AsyncMock(
        side_effect=[
            ClaudeResponse(
                text='{"hook": "Marktplaats moe?", "body": "Verkoop veilig op PeerMarket.", "cta": "Plaats nu"}',
                input_tokens=200,
                output_tokens=40,
                model="claude-sonnet-4-6",
                stop_reason="end_turn",
            ),
            ClaudeResponse(
                text='{"score": 92, "notes": "Spot on."}',
                input_tokens=300,
                output_tokens=20,
                model="claude-sonnet-4-6",
                stop_reason="end_turn",
            ),
        ]
    )

    draft_id = await run_draft_command(
        engine=prepared_db,
        claude=fake_claude,
        action_type_name="tiktok_post_organic",
        language="NL",
        theme="declutter",
    )
    assert draft_id is not None

    async with prepared_db.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT copy, brand_score, channel, language, status FROM drafts WHERE id = :id"
                ),
                {"id": draft_id},
            )
        ).fetchone()
    assert "Marktplaats moe?" in row[0]
    assert row[1] == 92
    assert row[2] == "tiktok"
    assert row[3] == "NL"
    assert row[4] == "queued"


async def test_run_draft_rejects_low_score_draft_does_not_persist(prepared_db):
    fake_claude = AsyncMock()
    fake_claude.complete = AsyncMock(
        side_effect=[
            ClaudeResponse(
                text='{"hook": "wrong tone", "body": "amazing offer!!!", "cta": "buy now!"}',
                input_tokens=200,
                output_tokens=40,
                model="claude-sonnet-4-6",
                stop_reason="end_turn",
            ),
            ClaudeResponse(
                text='{"score": 45, "notes": "off-brand exclamation, hype phrases."}',
                input_tokens=300,
                output_tokens=20,
                model="claude-sonnet-4-6",
                stop_reason="end_turn",
            ),
        ]
    )

    draft_id = await run_draft_command(
        engine=prepared_db,
        claude=fake_claude,
        action_type_name="tiktok_post_organic",
        language="NL",
        theme="declutter",
    )
    assert draft_id is None

    async with prepared_db.connect() as conn:
        count = (await conn.execute(text("SELECT count(*) FROM drafts"))).scalar()
    assert count == 0


async def test_run_draft_email_persists(prepared_db):
    fake_claude = AsyncMock()
    fake_claude.complete = AsyncMock(
        side_effect=[
            ClaudeResponse(
                text='{"subject": "Je hebt nog niets verkocht", "body": "Hoi, je hebt een account..."}',
                input_tokens=250,
                output_tokens=80,
                model="claude-sonnet-4-6",
                stop_reason="end_turn",
            ),
            ClaudeResponse(
                text='{"score": 85, "notes": "Good."}',
                input_tokens=300,
                output_tokens=20,
                model="claude-sonnet-4-6",
                stop_reason="end_turn",
            ),
        ]
    )

    draft_id = await run_draft_command(
        engine=prepared_db,
        claude=fake_claude,
        action_type_name="email_re_engagement",
        language="NL",
        audience="dormant_signups",
    )
    assert draft_id is not None


async def test_run_draft_seo_persists(prepared_db):
    fake_claude = AsyncMock()
    fake_claude.complete = AsyncMock(
        side_effect=[
            ClaudeResponse(
                text=(
                    '{"title": "Veilig tweedehands kopen — PeerMarket",'
                    ' "description": "Belgische marktplaats met geverifieerde verkopers. '
                    'Plaats je eerste item gratis."}'
                ),
                input_tokens=200,
                output_tokens=40,
                model="claude-sonnet-4-6",
                stop_reason="end_turn",
            ),
            ClaudeResponse(
                text='{"score": 88, "notes": "Good."}',
                input_tokens=300,
                output_tokens=20,
                model="claude-sonnet-4-6",
                stop_reason="end_turn",
            ),
        ]
    )
    draft_id = await run_draft_command(
        engine=prepared_db,
        claude=fake_claude,
        action_type_name="seo_pr",
        language="NL",
        page_path="/about",
        page_subject="who we are",
    )
    assert draft_id is not None
