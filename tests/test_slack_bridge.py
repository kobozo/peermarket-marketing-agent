"""Slack bridge handler tests — no real Slack."""

from types import SimpleNamespace
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


async def test_handle_im_with_ack_pattern_calls_ack_handler(monkeypatch, fake_say):
    from unittest.mock import AsyncMock

    from peermarket_agent.slack_bridge import app as bridge_app
    from peermarket_agent.slack_bridge.ack_handler import AckResult

    fake_engine = object()
    monkeypatch.setattr(bridge_app, "get_engine", lambda: fake_engine)

    fake_handle = AsyncMock(
        return_value=AckResult(
            success=True,
            reply_text="✅ Approved draft #42 (tiktok_post_organic). Trust score updated.",
        )
    )
    monkeypatch.setattr(bridge_app, "handle_ack", fake_handle)

    event = {"text": "✅ 42 lgtm", "user": "U0B5K95BRFV", "channel_type": "im"}
    await bridge_app.handle_im(event=event, say=fake_say)

    fake_handle.assert_awaited_once_with(
        fake_engine,
        action="approve",
        draft_id=42,
        decided_by="U0B5K95BRFV",
    )
    fake_say.assert_awaited_once()
    args, kwargs = fake_say.await_args
    assert "Approved draft #42" in (kwargs.get("text") or args[0])


async def test_handle_im_without_ack_falls_back_to_hello(monkeypatch, fake_say):
    from unittest.mock import AsyncMock

    from peermarket_agent.slack_bridge import app as bridge_app

    # Ensure handle_ack is NOT called when no ack pattern
    fake_handle = AsyncMock()
    monkeypatch.setattr(bridge_app, "handle_ack", fake_handle)

    event = {"text": "hi there", "user": "U0B5K95BRFV", "channel_type": "im"}
    await bridge_app.handle_im(event=event, say=fake_say)

    fake_handle.assert_not_called()
    fake_say.assert_awaited_once()
    args, kwargs = fake_say.await_args
    assert "PeerMarket marketing agent" in (kwargs.get("text") or args[0])


async def test_handle_im_dispatches_thread_video_upload_in_background(monkeypatch, fake_say):
    from peermarket_agent.slack_bridge import app as bridge_app

    fake_engine = object()
    dispatched = AsyncMock()
    monkeypatch.setattr(bridge_app, "get_engine", lambda: fake_engine)
    monkeypatch.setattr(
        bridge_app,
        "get_settings",
        lambda: SimpleNamespace(slack_founder_user_id="U123"),
    )
    monkeypatch.setattr(bridge_app, "_route_video_upload", dispatched)
    event = {
        "channel": "C123",
        "channel_type": "channel",
        "thread_ts": "1710000000.123456",
        "user": "U123",
        "files": [
            {
                "id": "F123",
                "name": "recording.mp4",
                "mimetype": "video/mp4",
                "size": 1234,
            }
        ],
    }

    await bridge_app.handle_im(event=event, say=fake_say)
    await __import__("asyncio").sleep(0)

    dispatched.assert_awaited_once()
    assert dispatched.await_args.args[0] is fake_engine
    assert dispatched.await_args.args[1].file_id == "F123"
    fake_say.assert_not_called()


@pytest.mark.parametrize("founder_user_id", ["", "U999"])
async def test_handle_im_routes_supported_video_from_unauthorized_uploader_for_thread_rejection(
    monkeypatch, fake_say, founder_user_id
):
    from peermarket_agent.slack_bridge import app as bridge_app

    fake_engine = object()
    dispatched = AsyncMock()
    monkeypatch.setattr(bridge_app, "get_engine", lambda: fake_engine)
    monkeypatch.setattr(
        bridge_app,
        "get_settings",
        lambda: SimpleNamespace(slack_founder_user_id=founder_user_id),
    )
    monkeypatch.setattr(bridge_app, "_route_video_upload", dispatched)
    event = {
        "channel": "C123",
        "channel_type": "channel",
        "thread_ts": "1710000000.123456",
        "user": "U123",
        "files": [
            {
                "id": "F123",
                "name": "recording.mp4",
                "mimetype": "video/mp4",
                "size": 1234,
            }
        ],
    }

    await bridge_app.handle_im(event=event, say=fake_say)
    await __import__("asyncio").sleep(0)

    dispatched.assert_awaited_once()
    fake_say.assert_not_called()
