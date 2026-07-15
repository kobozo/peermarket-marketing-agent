"""Hourly Slack outbox delivery loop."""

from sqlalchemy.ext.asyncio import AsyncEngine

from peermarket_agent.slack_notifier import SlackNotifier
from peermarket_agent.slack_outbox import deliver_pending_outbox


async def run_slack_outbox(engine: AsyncEngine, notifier: SlackNotifier) -> int:
    return await deliver_pending_outbox(engine, notifier)
