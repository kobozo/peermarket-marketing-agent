"""Read-only client for peermarket prod aggregate metrics.

Constrained to a fixed aggregate query — never SELECTs PII columns.
Returns a plain dict for the hourly loop. This will be wrapped in a
proper MCP stdio server in Phase 1.
"""

from dataclasses import dataclass
from datetime import date

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

_ATTRIBUTION_QUERY = text("""
SELECT day, utm_source, utm_medium, utm_campaign, utm_content,
       event_type, event_count
FROM marketing_attribution_daily
WHERE day >= :start AND day <= :stop
ORDER BY day, utm_content, event_type
""")


@dataclass(frozen=True)
class AttributionAggregate:
    day: date
    utm_source: str
    utm_medium: str
    utm_campaign: str
    utm_content: str
    event_type: str
    event_count: int


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

    async def fetch_attribution(self, start: date, stop: date) -> list[AttributionAggregate]:
        """Read campaign totals exclusively from the production aggregate view."""
        async with self._engine.connect() as conn:
            rows = (
                (await conn.execute(_ATTRIBUTION_QUERY, {"start": start, "stop": stop}))
                .mappings()
                .all()
            )
        return [AttributionAggregate(**dict(row)) for row in rows]
