"""Loop A — hourly pulse: heartbeat + peermarket KPI snapshot."""
from datetime import UTC, datetime

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

log = structlog.get_logger(__name__)


async def run_hourly_pulse(engine: AsyncEngine, peermarket) -> None:
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO kpis_hourly (ts, source, metric_name, value) "
                "VALUES (:ts, 'agent-internal', 'heartbeat', 1) "
                "ON CONFLICT (ts, source, metric_name) DO NOTHING"
            ),
            {"ts": now},
        )
        try:
            kpis = await peermarket.fetch_kpis()
            for name, value in kpis.items():
                await conn.execute(
                    text(
                        "INSERT INTO kpis_hourly (ts, source, metric_name, value) "
                        "VALUES (:ts, 'peermarket-prod', :n, :v) "
                        "ON CONFLICT (ts, source, metric_name) DO UPDATE "
                        "SET value = EXCLUDED.value"
                    ),
                    {"ts": now, "n": name, "v": float(value)},
                )
        except Exception:
            log.exception("hourly_pulse.peermarket_unreachable")
    log.info("hourly_pulse.complete", ts=now.isoformat())
