"""Drafts module tests — DB persistence + lookup helpers."""

import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from peermarket_agent.db.migrations import run_migrations
from peermarket_agent.db.seed import seed
from peermarket_agent.drafts import Draft, count_drafts_for_action, persist_draft


@pytest.fixture
async def engine():
    url = os.environ["AGENT_DB_URL"]
    eng = create_async_engine(url, future=True)
    async with eng.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await run_migrations(eng)
    await seed(eng)
    yield eng
    await eng.dispose()


async def test_persist_draft_writes_row(engine):
    draft = Draft(
        action_type_name="tiktok_post_organic",
        channel="tiktok",
        language="NL",
        copy="Marktplaats moe? Verkoop veilig op PeerMarket.",
        asset_path=None,
        generation_cost_cents=1,
        brand_score=92,
        visual_truthfulness_pass=True,
    )
    draft_id = await persist_draft(engine, draft)
    assert isinstance(draft_id, int)

    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT channel, language, copy, brand_score, status FROM drafts WHERE id = :id"
                ),
                {"id": draft_id},
            )
        ).fetchone()
    assert row[0] == "tiktok"
    assert row[1] == "NL"
    assert row[2] == "Marktplaats moe? Verkoop veilig op PeerMarket."
    assert row[3] == 92
    assert row[4] == "queued"


async def test_persist_draft_resolves_action_type_id(engine):
    draft = Draft(
        action_type_name="email_re_engagement",
        channel="email",
        language="NL",
        copy="Je hebt nog niets verkocht.",
        asset_path=None,
        generation_cost_cents=2,
        brand_score=88,
        visual_truthfulness_pass=True,
    )
    await persist_draft(engine, draft)

    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT at.name FROM drafts d "
                    "JOIN action_types at ON at.id = d.action_type_id "
                    "ORDER BY d.id DESC LIMIT 1"
                )
            )
        ).fetchone()
    assert row[0] == "email_re_engagement"


async def test_persist_draft_unknown_action_type_raises(engine):
    draft = Draft(
        action_type_name="not_a_real_action",
        channel="x",
        language="NL",
        copy="x",
        asset_path=None,
        generation_cost_cents=0,
        brand_score=0,
        visual_truthfulness_pass=True,
    )
    with pytest.raises(ValueError, match="unknown action_type"):
        await persist_draft(engine, draft)


async def test_count_drafts_for_action(engine):
    # 3 of one type, 1 of another
    for _ in range(3):
        await persist_draft(
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
            ),
        )
    await persist_draft(
        engine,
        Draft(
            action_type_name="email_re_engagement",
            channel="email",
            language="NL",
            copy="x",
            asset_path=None,
            generation_cost_cents=0,
            brand_score=80,
            visual_truthfulness_pass=True,
        ),
    )
    assert await count_drafts_for_action(engine, "tiktok_post_organic") == 3
    assert await count_drafts_for_action(engine, "email_re_engagement") == 1


async def test_persist_draft_stores_metadata(engine):
    metadata = {
        "audience_profile_key": "declutterers",
        "headline": "Verkoop veilig",
        "cta_type": "LEARN_MORE",
        "suggested_daily_budget_eur": 10,
    }
    draft_id = await persist_draft(
        engine,
        Draft(
            action_type_name="meta_ad_creative",
            channel="meta",
            language="NL",
            copy="x",
            asset_path=None,
            generation_cost_cents=2,
            brand_score=88,
            visual_truthfulness_pass=True,
            metadata=metadata,
        ),
    )
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT metadata FROM drafts WHERE id = :id"),
                {"id": draft_id},
            )
        ).fetchone()
    assert row[0] == metadata
