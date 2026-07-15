"""Slack bridge — socket-mode listener + FastAPI healthcheck on :8090."""

from __future__ import annotations

import asyncio
import contextlib
from functools import partial

import click
import structlog
from fastapi import FastAPI
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from uvicorn import Config, Server

from peermarket_agent.config import get_settings
from peermarket_agent.db.engine import get_engine
from peermarket_agent.slack_bridge.ack_handler import handle_ack
from peermarket_agent.slack_bridge.ack_parser import parse_ack
from peermarket_agent.slack_bridge.revision_handler import handle_revision_reply

log = structlog.get_logger(__name__)


_HELLO_TEXT = (
    "👋 PeerMarket marketing agent is online. I'm in Phase 0 — no posting yet, "
    "just confirming I can hear you."
)
_UNAUTHORIZED_TEXT = "This Slack user is not authorized to manage marketing drafts."


async def handle_app_mention(event: dict, say) -> None:
    if event.get("bot_id"):
        return
    await say(text=_HELLO_TEXT)


async def handle_im(
    event: dict,
    say,
    body: dict | None = None,
    *,
    founder_user_id: str,
) -> None:
    if (
        event.get("bot_id")
        or event.get("files")
        or event.get("subtype")
        in {
            "bot_message",
            "message_changed",
            "message_deleted",
            "thread_broadcast",
            "file_share",
        }
        or event.get("channel_type") != "im"
    ):
        return
    text_msg = event.get("text") or ""
    user_id = event.get("user", "unknown")
    if not founder_user_id or user_id != founder_user_id:
        kwargs = {"text": _UNAUTHORIZED_TEXT}
        if event.get("thread_ts"):
            kwargs["thread_ts"] = event["thread_ts"]
        await say(**kwargs)
        return
    parsed = parse_ack(text_msg)
    if parsed is not None:
        action, draft_id = parsed
        engine = get_engine()
        result = await handle_ack(engine, action=action, draft_id=draft_id, decided_by=user_id)
        kwargs = {"text": result.reply_text}
        if event.get("thread_ts"):
            kwargs["thread_ts"] = event["thread_ts"]
        await say(**kwargs)
        return
    if event.get("thread_ts"):
        engine = get_engine()
        routed_event = dict(event)
        if body and body.get("event_id"):
            routed_event["event_id"] = body["event_id"]
        result = await handle_revision_reply(engine, routed_event, founder_user_id)
        if result.reply_text:
            try:
                await say(text=result.reply_text, thread_ts=event["thread_ts"])
            except Exception as error:
                log.warning(
                    "slack_bridge.revision_receipt_failed",
                    event_id=routed_event.get("event_id"),
                    failure_category=type(error).__name__,
                )
        return
    await say(text=_HELLO_TEXT)


def build_app(slack_bot_token: str, founder_user_id: str) -> AsyncApp:
    app = AsyncApp(token=slack_bot_token)
    app.event("app_mention")(handle_app_mention)
    app.event("message")(partial(handle_im, founder_user_id=founder_user_id))
    return app


def build_healthz_api() -> FastAPI:
    api = FastAPI()

    @api.get("/agent/healthz")
    async def healthz() -> dict:
        return {"status": "ok", "service": "slack-bridge"}

    return api


async def _run() -> None:
    settings = get_settings()
    app = build_app(settings.slack_bot_token, settings.slack_founder_user_id)
    handler = AsyncSocketModeHandler(app, settings.slack_app_token)

    api = build_healthz_api()
    config = Config(api, host="127.0.0.1", port=settings.healthcheck_port, log_level="warning")
    server = Server(config)

    log.info("slack_bridge.start", port=settings.healthcheck_port)
    await asyncio.gather(handler.start_async(), server.serve())


@click.command()
def cli() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run())


if __name__ == "__main__":
    cli()
