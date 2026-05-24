"""Marketing agent entrypoint — runs migrations, seed, then loops."""
import asyncio
import contextlib
from datetime import UTC, datetime, timedelta

import click
import structlog

from peermarket_agent.agent.loops.hourly import run_hourly_pulse
from peermarket_agent.config import get_settings
from peermarket_agent.db.engine import get_engine
from peermarket_agent.db.migrations import run_migrations
from peermarket_agent.db.seed import seed

log = structlog.get_logger(__name__)


async def _sleep_until_next_hour() -> None:
    now = datetime.now(UTC)
    next_hour = (now + timedelta(hours=1)).replace(minute=0, second=5, microsecond=0)
    secs = (next_hour - now).total_seconds()
    log.info("agent.sleep_until_next_hour", seconds=int(secs))
    await asyncio.sleep(secs)


async def _run() -> None:
    # Lazy import: PeermarketReadonly is delivered in T8. Keeping it inside
    # _run() means pytest collection of this module doesn't fail before T8
    # lands.
    from peermarket_agent.mcp_servers.peermarket_readonly import PeermarketReadonly

    settings = get_settings()
    engine = get_engine()
    await run_migrations(engine)
    await seed(engine)
    peermarket = PeermarketReadonly(settings.peermarket_prod_db_readonly_url)
    log.info("agent.start", env="phase-0")

    # One-shot pulse on startup so smoke tests have data immediately.
    await run_hourly_pulse(engine, peermarket)

    while True:
        await _sleep_until_next_hour()
        try:
            await run_hourly_pulse(engine, peermarket)
        except Exception:
            log.exception("agent.hourly_pulse_failed")


@click.command()
def cli() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run())


if __name__ == "__main__":
    cli()
