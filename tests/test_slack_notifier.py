"""Slack notifier tests — no real Slack calls."""

from unittest.mock import AsyncMock

from peermarket_agent.slack_notifier import SlackMessageResult, SlackNotifier


def _notifier_with_recorder(monkeypatch) -> tuple[SlackNotifier, dict]:
    """Build a SlackNotifier whose chat_postMessage call kwargs are captured."""
    recorded: dict = {}

    async def fake_post_message(**kwargs):
        recorded.update(kwargs)
        return {"ok": True, "channel": kwargs.get("channel", "D456"), "ts": "1720000000.123456"}

    fake_client = AsyncMock()
    fake_client.chat_postMessage = AsyncMock(side_effect=fake_post_message)
    monkeypatch.setattr(
        "peermarket_agent.slack_notifier.AsyncWebClient",
        lambda *a, **kw: fake_client,
    )
    notifier = SlackNotifier(bot_token="xoxb-test", founder_user_id="U123")
    return notifier, recorded


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


async def test_send_message_forwards_blocks(monkeypatch) -> None:
    notifier, recorded = _notifier_with_recorder(monkeypatch)
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}]

    await notifier.send_message("fallback", channel_id="C123", blocks=blocks)

    assert recorded["blocks"] == blocks
    assert recorded["text"] == "fallback"


async def test_send_message_omits_blocks_kwarg_when_absent(monkeypatch) -> None:
    notifier, recorded = _notifier_with_recorder(monkeypatch)

    await notifier.send_message("plain", channel_id="C123")

    assert "blocks" not in recorded


async def test_notify_founder_forwards_blocks(monkeypatch) -> None:
    notifier, recorded = _notifier_with_recorder(monkeypatch)
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}]

    await notifier.notify_founder("fallback", blocks=blocks)

    assert recorded["blocks"] == blocks
    assert recorded["text"] == "fallback"


async def test_notify_founder_omits_blocks_kwarg_when_absent(monkeypatch) -> None:
    notifier, recorded = _notifier_with_recorder(monkeypatch)

    await notifier.notify_founder("plain")

    assert "blocks" not in recorded
