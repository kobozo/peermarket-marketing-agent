"""Brand voice loader + DB sync tests."""

import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from peermarket_agent.db.migrations import run_migrations
from peermarket_agent.db.seed import seed
from peermarket_agent.prompts.brand_voice import (
    BRAND_VOICE_FILE,
    load_brand_voice,
    sync_to_db,
)


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


def test_brand_voice_file_exists():
    assert BRAND_VOICE_FILE.exists(), f"brand_voice.md missing at {BRAND_VOICE_FILE}"


def test_load_brand_voice_returns_markdown():
    md = load_brand_voice()
    assert isinstance(md, str)
    assert "# PeerMarket brand voice" in md
    assert "Visual truthfulness" in md


def test_load_brand_voice_includes_approved_examples():
    md = load_brand_voice()
    assert "Marktplaats moe?" in md  # NL approved example


async def test_sync_to_db_overwrites_brand_voice_row(engine):
    # seed() set a placeholder; sync should now write the real markdown.
    await sync_to_db(engine)
    async with engine.connect() as conn:
        row = (
            await conn.execute(text("SELECT voice_rules_md FROM brand_voice WHERE id=1"))
        ).fetchone()
    assert row is not None
    assert "# PeerMarket brand voice" in row[0]
    assert "Marktplaats moe?" in row[0]


async def test_sync_to_db_is_idempotent(engine):
    await sync_to_db(engine)
    await sync_to_db(engine)
    async with engine.connect() as conn:
        count = (await conn.execute(text("SELECT count(*) FROM brand_voice"))).scalar()
    assert count == 1
