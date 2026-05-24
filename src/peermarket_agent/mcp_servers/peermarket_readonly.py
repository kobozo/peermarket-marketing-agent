"""Read-only client for peermarket prod KPIs.

Constrained to a fixed aggregate query — never SELECTs PII columns.
Returns a plain dict for the hourly loop. This will be wrapped in a
proper MCP stdio server in Phase 1.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

# Aggregate-only. No user rows, no PII. Counts only.
_KPI_QUERY = text("""
SELECT
    (SELECT COUNT(*) FROM users
        WHERE created_at > NOW() - INTERVAL '24 hours')      AS signups_24h,
    (SELECT COUNT(*) FROM listings
        WHERE created_at > NOW() - INTERVAL '24 hours')      AS listings_24h,
    (SELECT COUNT(DISTINCT owner_id) FROM listings
        WHERE created_at > NOW() - INTERVAL '30 days')       AS active_sellers_30d
""")


class PeermarketReadonly:
    def __init__(self, dsn: str) -> None:
        self._engine = create_async_engine(dsn, future=True, pool_pre_ping=True)

    async def fetch_kpis(self) -> dict[str, int]:
        async with self._engine.connect() as conn:
            row = (await conn.execute(_KPI_QUERY)).fetchone()
        return {
            "signups_24h": int(row[0]),
            "listings_24h": int(row[1]),
            "active_sellers_30d": int(row[2]),
        }
