"""Peermarket prod read-only client tests — fetches whitelisted KPIs only."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from peermarket_agent.mcp_servers.peermarket_readonly import PeermarketReadonly


@pytest.fixture
def fake_engine(monkeypatch):
    """Patch the engine factory to return a mock that records queries."""
    fake = MagicMock()
    conn = AsyncMock()
    conn.__aenter__.return_value = conn
    conn.__aexit__.return_value = None
    result = MagicMock()
    result.fetchone.return_value = (12, 34, 5)
    conn.execute = AsyncMock(return_value=result)
    fake.connect.return_value = conn

    monkeypatch.setattr(
        "peermarket_agent.mcp_servers.peermarket_readonly.create_async_engine",
        lambda *a, **kw: fake,
    )
    return fake, conn


async def test_fetch_kpis_returns_dict(fake_engine):
    fake, conn = fake_engine
    client = PeermarketReadonly("postgresql+asyncpg://ro:x@host/peer")
    kpis = await client.fetch_kpis()
    assert isinstance(kpis, dict)
    assert "signups_24h" in kpis
    assert "listings_24h" in kpis
    assert "active_sellers_30d" in kpis


async def test_fetch_kpis_executes_only_whitelisted_query(fake_engine):
    fake, conn = fake_engine
    client = PeermarketReadonly("postgresql+asyncpg://ro:x@host/peer")
    await client.fetch_kpis()
    # exactly one execute call, on the whitelisted aggregate query
    assert conn.execute.await_count == 1
    sql_arg = str(conn.execute.await_args.args[0])
    assert "COUNT" in sql_arg.upper()
    # forbidden patterns
    assert "users.email" not in sql_arg.lower()
    assert "phone" not in sql_arg.lower()
