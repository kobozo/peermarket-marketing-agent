"""Marketing agent entrypoint — runs migrations, seed, then loops."""

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta

import click
import structlog

from peermarket_agent.agent.loops.daily import (
    _seconds_until_next_9am,
    run_daily_drafts,
)
from peermarket_agent.agent.loops.hourly import run_hourly_pulse
from peermarket_agent.agent.loops.performance_daily import run_daily_performance
from peermarket_agent.agent.loops.revisions import run_pending_revisions
from peermarket_agent.agent.loops.slack_outbox import run_slack_outbox
from peermarket_agent.claude import ClaudeClient
from peermarket_agent.config import get_settings
from peermarket_agent.db.engine import get_engine
from peermarket_agent.db.migrations import run_migrations
from peermarket_agent.db.seed import seed
from peermarket_agent.mcp_servers.peermarket_readonly import PeermarketReadonly
from peermarket_agent.prompts.brand_voice import sync_to_db as sync_brand_voice
from peermarket_agent.slack_notifier import SlackNotifier

log = structlog.get_logger(__name__)


async def _sleep_until_next_hour() -> None:
    now = datetime.now(UTC)
    next_hour = (now + timedelta(hours=1)).replace(minute=0, second=5, microsecond=0)
    secs = (next_hour - now).total_seconds()
    log.info("agent.sleep_until_next_hour", seconds=int(secs))
    await asyncio.sleep(secs)


async def _hourly_forever(
    engine, peermarket, notifier: SlackNotifier, settings=None, claude: ClaudeClient | None = None
) -> None:
    while True:
        await _sleep_until_next_hour()
        try:
            await run_hourly_pulse(
                engine, peermarket, settings=settings, notifier=notifier, claude=claude
            )
        except Exception:
            log.exception("agent.hourly_pulse_failed")
        try:
            await run_slack_outbox(engine, notifier)
        except Exception:
            log.exception("agent.slack_outbox_failed")


async def _revisions_forever(engine, claude: ClaudeClient, notifier: SlackNotifier) -> None:
    while True:
        await asyncio.sleep(15)
        try:
            await run_pending_revisions(engine, claude, notifier)
        except Exception:
            log.exception("agent.revisions_failed")


async def _daily_forever(
    engine, claude: ClaudeClient, notifier: SlackNotifier, settings=None
) -> None:
    while True:
        secs = await _seconds_until_next_9am()
        log.info("agent.sleep_until_next_9am", seconds=int(secs))
        await asyncio.sleep(secs)
        try:
            await run_daily_drafts(engine=engine, claude=claude, notifier=notifier)
        except Exception:
            log.exception("agent.daily_loop_failed")
        try:
            await run_daily_performance(engine, notifier, settings)
        except Exception:
            log.exception("agent.daily_performance_failed")


async def _run_startup_jobs(
    engine, peermarket, notifier: SlackNotifier, claude: ClaudeClient | None = None, settings=None
) -> None:
    try:
        await run_slack_outbox(engine, notifier)
    except Exception:
        log.exception("agent.startup_slack_outbox_failed")
    try:
        await run_hourly_pulse(
            engine, peermarket, settings=settings, notifier=notifier, claude=claude
        )
    except Exception:
        log.exception("agent.startup_hourly_pulse_failed")
    if claude is not None:
        try:
            await run_pending_revisions(engine, claude, notifier)
        except Exception:
            log.exception("agent.startup_revisions_failed")


async def _run() -> None:
    settings = get_settings()
    engine = get_engine()
    await run_migrations(engine)
    await seed(engine)
    await sync_brand_voice(engine)
    peermarket = PeermarketReadonly(settings.peermarket_prod_db_readonly_url)
    claude = ClaudeClient(api_key=settings.anthropic_api_key)
    notifier = SlackNotifier(
        bot_token=settings.slack_bot_token,
        founder_user_id=settings.slack_founder_user_id,
    )
    log.info("agent.start", env="phase-1-loop-b-mvp")

    # Independent one-shot retries/pulse; neither can prevent recurring loops.
    await _run_startup_jobs(engine, peermarket, notifier, claude, settings)

    # Hourly KPI pulse + daily 09:00 Brussels draft loop, both forever.
    await asyncio.gather(
        _hourly_forever(engine, peermarket, notifier, settings, claude),
        _daily_forever(engine, claude, notifier, settings),
        _revisions_forever(engine, claude, notifier),
    )


@click.command()
def cli() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run())


if __name__ == "__main__":
    cli()
