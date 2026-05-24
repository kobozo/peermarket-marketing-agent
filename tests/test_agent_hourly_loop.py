"""Hourly Loop A — writes a row to kpis_hourly."""
import os
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from peermarket_agent.agent.loops.hourly import run_hourly_pulse
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
    await seed(eng)
    yield eng
    await eng.dispose()


async def test_hourly_pulse_writes_heartbeat_row(engine):
    fake_peermarket = AsyncMock()
    fake_peermarket.fetch_kpis.return_value = {"signups": 3, "listings": 7}

    await run_hourly_pulse(engine=engine, peermarket=fake_peermarket)

    async with engine.connect() as conn:
        rows = (await conn.execute(text(
            "SELECT source, metric_name, value FROM kpis_hourly ORDER BY metric_name"
        ))).fetchall()
    metrics = {(r[0], r[1]): float(r[2]) for r in rows}
    assert metrics[("agent-internal", "heartbeat")] == 1.0
    assert metrics[("peermarket-prod", "signups")] == 3.0
    assert metrics[("peermarket-prod", "listings")] == 7.0


async def test_hourly_pulse_writes_heartbeat_when_peermarket_unavailable(engine):
    fake_peermarket = AsyncMock()
    fake_peermarket.fetch_kpis.side_effect = RuntimeError("connection refused")

    await run_hourly_pulse(engine=engine, peermarket=fake_peermarket)

    async with engine.connect() as conn:
        rows = (await conn.execute(text(
            "SELECT source, metric_name FROM kpis_hourly"
        ))).fetchall()
    sources = {(r[0], r[1]) for r in rows}
    # heartbeat still written; peermarket metrics absent
    assert ("agent-internal", "heartbeat") in sources
    assert ("peermarket-prod", "signups") not in sources
