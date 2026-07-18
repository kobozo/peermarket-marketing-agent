"""Slack bridge handler tests — no real Slack."""

from unittest.mock import AsyncMock

import pytest

from peermarket_agent.slack_bridge.app import (
    build_app,
    handle_app_mention,
    handle_im,
    is_authorized_user,
)


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
    await handle_im(event=event, say=fake_say, founder_user_id="U999")
    fake_say.assert_awaited_once()


async def test_handle_im_ignores_bot_messages(fake_say):
    event = {"text": "hi", "user": "U999", "bot_id": "B000", "channel_type": "im"}
    await handle_im(event=event, say=fake_say, founder_user_id="U999")
    fake_say.assert_not_called()


def test_slack_chat_authorizes_founder_and_configured_members(monkeypatch):
    monkeypatch.setenv("SLACK_FOUNDER_USER_ID", "UFOUNDER")
    monkeypatch.setenv("SLACK_AGENT_ALLOWED_USER_IDS", "UTEAM1,UTEAM2")
    assert is_authorized_user("UFOUNDER")
    assert is_authorized_user("UTEAM2")
    assert not is_authorized_user("UOTHER")


async def test_handle_im_routes_natural_feedback_to_jarvis(monkeypatch, fake_say):
    from peermarket_agent.slack_bridge import app as bridge_app

    claude = AsyncMock()
    claude.complete.return_value = type(
        "Response", (), {"text": "Ik onderzoek dit en kom met een plan."}
    )()
    monkeypatch.setattr(bridge_app, "is_authorized_user", lambda *args: True)
    event = {
        "text": "Ik ben niet blij met de conversie; zoek uit hoe dit beter kan.",
        "user": "U0B5K95BRFV",
        "channel_type": "im",
        "ts": "1.0",
    }
    await bridge_app.handle_im(
        event=event, say=fake_say, founder_user_id="U0B5K95BRFV", claude=claude
    )

    claude.complete.assert_awaited_once()
    args, kwargs = fake_say.await_args
    assert "onderzoek" in (kwargs.get("text") or args[0])


def test_build_app_returns_async_app(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "sig")
    from slack_bolt.async_app import AsyncApp

    app = build_app(slack_bot_token="xoxb-test", founder_user_id="U999")
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
    await bridge_app.handle_im(event=event, say=fake_say, founder_user_id="U0B5K95BRFV")

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
    await bridge_app.handle_im(event=event, say=fake_say, founder_user_id="U0B5K95BRFV")

    fake_handle.assert_not_called()
    fake_say.assert_awaited_once()
    args, kwargs = fake_say.await_args
    assert "PeerMarket marketing agent" in (kwargs.get("text") or args[0])


async def test_thread_ack_has_precedence_over_revision_routing(monkeypatch, fake_say):
    from peermarket_agent.slack_bridge import app as bridge_app
    from peermarket_agent.slack_bridge.ack_handler import AckResult

    fake_engine = object()
    monkeypatch.setattr(bridge_app, "get_engine", lambda: fake_engine)
    ack = AsyncMock(return_value=AckResult(success=True, reply_text="approved"))
    revision = AsyncMock()
    monkeypatch.setattr(bridge_app, "handle_ack", ack)
    monkeypatch.setattr(bridge_app, "handle_revision_reply", revision)

    await bridge_app.handle_im(
        event={
            "text": "✅ 42",
            "user": "U1",
            "channel": "D1",
            "channel_type": "im",
            "thread_ts": "100.000",
            "ts": "100.001",
        },
        say=fake_say,
        body={"event_id": "Ev1"},
        founder_user_id="U1",
    )

    ack.assert_awaited_once()
    revision.assert_not_called()
    fake_say.assert_awaited_once_with(text="approved", thread_ts="100.000")


async def test_handled_thread_feedback_acks_in_thread_without_hello(monkeypatch, fake_say):
    from peermarket_agent.slack_bridge import app as bridge_app
    from peermarket_agent.slack_bridge.revision_handler import RevisionReplyResult

    fake_engine = object()
    monkeypatch.setattr(bridge_app, "get_engine", lambda: fake_engine)
    revision = AsyncMock(
        return_value=RevisionReplyResult(kind="recorded", reply_text="Feedback received.")
    )
    monkeypatch.setattr(bridge_app, "handle_revision_reply", revision)
    event = {
        "text": "Shorter",
        "user": "U1",
        "channel": "D1",
        "channel_type": "im",
        "thread_ts": "100.000",
        "ts": "100.001",
    }

    await bridge_app.handle_im(
        event=event,
        say=fake_say,
        body={"event_id": "Ev1"},
        founder_user_id="U1",
    )

    revision.assert_awaited_once()
    routed_event = revision.await_args.args[1]
    assert routed_event["event_id"] == "Ev1"
    fake_say.assert_awaited_once_with(text="Feedback received.", thread_ts="100.000")


async def test_ignored_thread_event_does_not_fall_back_to_hello(monkeypatch, fake_say):
    from peermarket_agent.slack_bridge import app as bridge_app
    from peermarket_agent.slack_bridge.revision_handler import RevisionReplyResult

    monkeypatch.setattr(bridge_app, "get_engine", lambda: object())
    monkeypatch.setattr(
        bridge_app,
        "handle_revision_reply",
        AsyncMock(return_value=RevisionReplyResult(kind="ignored")),
    )

    await bridge_app.handle_im(
        event={
            "text": "bot prose",
            "bot_id": "B1",
            "channel": "D1",
            "channel_type": "im",
            "thread_ts": "100.000",
            "ts": "100.001",
        },
        say=fake_say,
        body={"event_id": "Ev1"},
        founder_user_id="U1",
    )

    fake_say.assert_not_called()


async def test_thread_file_caption_is_ignored_before_ack_or_revision(monkeypatch, fake_say):
    from peermarket_agent.slack_bridge import app as bridge_app

    ack = AsyncMock()
    revision = AsyncMock()
    monkeypatch.setattr(bridge_app, "handle_ack", ack)
    monkeypatch.setattr(bridge_app, "handle_revision_reply", revision)

    await bridge_app.handle_im(
        event={
            "text": "✅ 42 make this shorter",
            "files": [{"id": "F1"}],
            "user": "U1",
            "channel": "D1",
            "channel_type": "im",
            "thread_ts": "100.000",
            "ts": "100.001",
        },
        say=fake_say,
        body={"event_id": "Ev-file"},
        founder_user_id="U1",
    )

    ack.assert_not_called()
    revision.assert_not_called()
    fake_say.assert_not_called()


async def test_unauthorized_thread_ack_is_refused_without_database_access(monkeypatch, fake_say):
    from peermarket_agent.slack_bridge import app as bridge_app

    get_engine = AsyncMock()
    ack = AsyncMock()
    monkeypatch.setattr(bridge_app, "get_engine", get_engine)
    monkeypatch.setattr(bridge_app, "handle_ack", ack)

    await bridge_app.handle_im(
        event={
            "text": "✅ 42",
            "user": "U-other",
            "channel": "D1",
            "channel_type": "im",
            "thread_ts": "100.000",
            "ts": "100.001",
        },
        say=fake_say,
        founder_user_id="U-founder",
    )

    get_engine.assert_not_called()
    ack.assert_not_called()
    assert "not authorized" in fake_say.await_args.kwargs["text"].lower()
    assert "42" not in fake_say.await_args.kwargs["text"]


async def test_unauthorized_thread_revision_is_refused_without_mutation(monkeypatch, fake_say):
    from peermarket_agent.slack_bridge import app as bridge_app

    revision = AsyncMock()
    monkeypatch.setattr(bridge_app, "handle_revision_reply", revision)

    await bridge_app.handle_im(
        event={
            "text": "Make confidential launch copy punchier",
            "user": "U-other",
            "channel": "D1",
            "channel_type": "im",
            "thread_ts": "100.000",
            "ts": "100.001",
        },
        say=fake_say,
        founder_user_id="U-founder",
    )

    revision.assert_not_called()
    refusal = fake_say.await_args.kwargs["text"]
    assert "not authorized" in refusal.lower()
    assert "confidential" not in refusal.lower()
