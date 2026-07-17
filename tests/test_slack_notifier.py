"""Slack notifier tests — no real Slack calls."""

from unittest.mock import AsyncMock

from peermarket_agent.slack_notifier import SlackMessageResult, SlackNotifier


async def test_send_message_returns_slack_channel_and_timestamp(monkeypatch):
    fake_client = AsyncMock()
    fake_client.chat_postMessage = AsyncMock(
        return_value={"ok": True, "channel": "D456", "ts": "1720000000.123456"}
    )
    monkeypatch.setattr(
        "peermarket_agent.slack_notifier.AsyncWebClient",
        lambda *a, **kw: fake_client,
    )
    notifier = SlackNotifier(bot_token="xoxb-test", founder_user_id="U123")

    result = await notifier.send_message("approval", channel_id="D456")

    assert result == SlackMessageResult(channel_id="D456", ts="1720000000.123456")
    fake_client.chat_postMessage.assert_awaited_once_with(channel="D456", text="approval")


async def test_send_message_posts_thread_reply_to_explicit_root(monkeypatch):
    fake_client = AsyncMock()
    fake_client.chat_postMessage = AsyncMock(
        return_value={"ok": True, "channel": "D456", "ts": "1720000001.000001"}
    )
    monkeypatch.setattr(
        "peermarket_agent.slack_notifier.AsyncWebClient",
        lambda *a, **kw: fake_client,
    )
    notifier = SlackNotifier(bot_token="xoxb-test", founder_user_id="U123")

    await notifier.send_message("revision", channel_id="D456", thread_ts="1720000000.123456")

    fake_client.chat_postMessage.assert_awaited_once_with(
        channel="D456", text="revision", thread_ts="1720000000.123456"
    )


async def test_notify_founder_posts_dm(monkeypatch):
    fake_client = AsyncMock()
    fake_client.chat_postMessage = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(
        "peermarket_agent.slack_notifier.AsyncWebClient",
        lambda *a, **kw: fake_client,
    )
    notifier = SlackNotifier(bot_token="xoxb-test", founder_user_id="U123")
    sent = await notifier.notify_founder("hello")
    assert sent is True
    fake_client.chat_postMessage.assert_awaited_once_with(channel="U123", text="hello")


async def test_notify_founder_no_id_does_nothing(monkeypatch):
    fake_client = AsyncMock()
    monkeypatch.setattr(
        "peermarket_agent.slack_notifier.AsyncWebClient",
        lambda *a, **kw: fake_client,
    )
    notifier = SlackNotifier(bot_token="xoxb-test", founder_user_id="")
    sent = await notifier.notify_founder("hello")
    assert sent is False
    fake_client.chat_postMessage.assert_not_called()


async def test_notify_founder_handles_slack_errors(monkeypatch):
    fake_client = AsyncMock()
    fake_client.chat_postMessage = AsyncMock(side_effect=RuntimeError("slack down"))
    monkeypatch.setattr(
        "peermarket_agent.slack_notifier.AsyncWebClient",
        lambda *a, **kw: fake_client,
    )
    notifier = SlackNotifier(bot_token="xoxb-test", founder_user_id="U123")
    sent = await notifier.notify_founder("hello")
    assert sent is False


async def test_post_draft_thread_returns_root_message_reference(monkeypatch):
    fake_client = AsyncMock()
    fake_client.chat_postMessage = AsyncMock(
        return_value={"ok": True, "channel": "C123", "ts": "1710000000.123456"}
    )
    monkeypatch.setattr(
        "peermarket_agent.slack_notifier.AsyncWebClient",
        lambda *a, **kw: fake_client,
    )
    notifier = SlackNotifier(bot_token="xoxb-test", founder_user_id="U123")

    reference = await notifier.post_draft_thread(42, "Record this script in a thread.")

    assert reference == ("C123", "1710000000.123456")
    fake_client.chat_postMessage.assert_awaited_once_with(
        channel="U123", text="Record this script in a thread."
    )
