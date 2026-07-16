"""Hourly Slack outbox loop tests."""

from unittest.mock import AsyncMock

from peermarket_agent.agent.loops.slack_outbox import run_slack_outbox


async def test_run_slack_outbox_delivers_due_messages(monkeypatch):
    deliver = AsyncMock(return_value=3)
    monkeypatch.setattr("peermarket_agent.agent.loops.slack_outbox.deliver_pending_outbox", deliver)
    engine = object()
    notifier = object()

    assert await run_slack_outbox(engine, notifier) == 3
    deliver.assert_awaited_once_with(engine, notifier)
