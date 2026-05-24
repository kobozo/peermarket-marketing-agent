"""Slack bridge handler tests — no real Slack."""

from unittest.mock import AsyncMock

import pytest

from peermarket_agent.slack_bridge.app import build_app, handle_app_mention, handle_im


@pytest.fixture
def fake_say():
    return AsyncMock()


async def test_handle_app_mention_responds_with_hello(fake_say):
    event = {"text": "<@U123> hello", "user": "U999", "ts": "1.0"}
    await handle_app_mention(event=event, say=fake_say)
    fake_say.assert_awaited_once()
    args, kwargs = fake_say.await_args
    assert "PeerMarket marketing agent" in (kwargs.get("text") or args[0])


async def test_handle_im_responds_with_hello(fake_say):
    event = {"text": "hi", "user": "U999", "channel_type": "im", "ts": "1.0"}
    await handle_im(event=event, say=fake_say)
    fake_say.assert_awaited_once()


async def test_handle_im_ignores_bot_messages(fake_say):
    event = {"text": "hi", "user": "U999", "bot_id": "B000", "channel_type": "im"}
    await handle_im(event=event, say=fake_say)
    fake_say.assert_not_called()


def test_build_app_returns_async_app(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "sig")
    from slack_bolt.async_app import AsyncApp

    app = build_app(slack_bot_token="xoxb-test")
    assert isinstance(app, AsyncApp)
