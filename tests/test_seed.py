"""Seed data tests."""
import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from peermarket_agent.db.migrations import run_migrations
from peermarket_agent.db.seed import seed


@pytest.fixture
async def engine():
    url = os.environ["AGENT_DB_URL"]
    eng = create_async_engine(url, future=True)
    async with eng.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await run_migrations(eng)
    yield eng
    await eng.dispose()


async def test_seed_inserts_action_types(engine):
    await seed(engine)
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT count(*) FROM action_types"))
        assert result.scalar() >= 10


async def test_seed_creates_trust_scores_for_each_action_type(engine):
    await seed(engine)
    async with engine.connect() as conn:
        result = await conn.execute(text(
            "SELECT count(*) FROM action_types a "
            "LEFT JOIN trust_scores t ON t.action_type_id = a.id "
            "WHERE t.action_type_id IS NULL"
        ))
        assert result.scalar() == 0  # every action_type has a row


async def test_seed_is_idempotent(engine):
    await seed(engine)
    await seed(engine)
    async with engine.connect() as conn:
        result = await conn.execute(text(
            "SELECT count(*) FROM action_types WHERE name='tiktok_post_organic'"
        ))
        assert result.scalar() == 1


async def test_seed_brand_voice_singleton(engine):
    await seed(engine)
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT count(*) FROM brand_voice"))
        assert result.scalar() == 1
