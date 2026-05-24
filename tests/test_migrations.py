"""Migration runner tests — idempotency + schema shape."""
import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from peermarket_agent.db.migrations import run_migrations

REQUIRED_TABLES = {
    "schema_version", "brand_voice", "action_types", "trust_scores",
    "drafts", "publications", "budget_ledger", "kpis_hourly",
    "slack_actions", "strategy_memos", "creatives_archive",
    "self_extensions", "learnings",
}


@pytest.fixture
async def engine():
    url = os.environ["AGENT_DB_URL"]
    eng = create_async_engine(url, future=True)
    async with eng.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    yield eng
    await eng.dispose()


async def test_migrations_create_all_expected_tables(engine):
    await run_migrations(engine)
    async with engine.connect() as conn:
        result = await conn.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname='public'"
        ))
        tables = {row[0] for row in result.fetchall()}
    missing = REQUIRED_TABLES - tables
    assert not missing, f"missing tables: {missing}"


async def test_migrations_are_idempotent(engine):
    await run_migrations(engine)
    await run_migrations(engine)  # second run must not raise
    async with engine.connect() as conn:
        result = await conn.execute(text(
            "SELECT count(*) FROM action_types"
        ))
        # Schema-only — seed lives in T4. Count is 0 here.
        assert result.scalar() == 0


async def test_pgvector_extension_enabled(engine):
    await run_migrations(engine)
    async with engine.connect() as conn:
        result = await conn.execute(text(
            "SELECT extname FROM pg_extension WHERE extname='vector'"
        ))
        assert result.scalar() == "vector"


async def test_creatives_archive_has_vector_column(engine):
    await run_migrations(engine)
    async with engine.connect() as conn:
        result = await conn.execute(text(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name='creatives_archive' AND column_name='embedding'"
        ))
        # pgvector reports as 'USER-DEFINED'
        assert result.scalar() == "USER-DEFINED"
