"""Slack bridge — socket-mode listener + FastAPI healthcheck on :8090."""

from __future__ import annotations

import asyncio
import contextlib

import click
import structlog
from fastapi import FastAPI
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from slack_sdk.web.async_client import AsyncWebClient
from uvicorn import Config, Server

from peermarket_agent.claude import ClaudeClient
from peermarket_agent.config import get_settings
from peermarket_agent.db.engine import get_engine
from peermarket_agent.slack_bridge.ack_handler import handle_ack
from peermarket_agent.slack_bridge.ack_parser import parse_ack
from peermarket_agent.slack_bridge.video_events import (
    VideoUpload,
    extract_video_upload,
)
from peermarket_agent.video_workflow import process_video_upload

log = structlog.get_logger(__name__)


_HELLO_TEXT = (
    "👋 PeerMarket marketing agent is online. I'm in Phase 0 — no posting yet, "
    "just confirming I can hear you."
)


async def handle_app_mention(event: dict, say) -> None:
    if event.get("bot_id"):
        return
    await say(text=_HELLO_TEXT)


async def handle_im(event: dict, say) -> None:
    if event.get("bot_id"):
        return
    upload = extract_video_upload(event)
    if upload is not None:
        asyncio.create_task(_route_video_upload(get_engine(), upload))
        return
    if event.get("channel_type") != "im":
        return
    text_msg = event.get("text") or ""
    user_id = event.get("user", "unknown")
    parsed = parse_ack(text_msg)
    if parsed is not None:
        action, draft_id = parsed
        engine = get_engine()
        result = await handle_ack(engine, action=action, draft_id=draft_id, decided_by=user_id)
        await say(text=result.reply_text)
        return
    await say(text=_HELLO_TEXT)


async def _route_video_upload(engine, upload: VideoUpload) -> None:
    """Run the secure media workflow in the background for one Slack event."""
    settings = get_settings()
    result = await process_video_upload(
        engine,
        AsyncWebClient(token=settings.slack_bot_token),
        ClaudeClient(api_key=settings.anthropic_api_key),
        upload,
        settings,
    )
    log.info(
        "slack_video.processed",
        file_id=upload.file_id,
        status=result.status,
        output_path=str(result.output_path) if result.output_path else None,
    )


def build_app(slack_bot_token: str) -> AsyncApp:
    app = AsyncApp(token=slack_bot_token)
    app.event("app_mention")(handle_app_mention)
    app.event("message")(handle_im)
    return app


def build_healthz_api() -> FastAPI:
    api = FastAPI()

    @api.get("/agent/healthz")
    async def healthz() -> dict:
        return {"status": "ok", "service": "slack-bridge"}

    return api


async def _run() -> None:
    settings = get_settings()
    app = build_app(settings.slack_bot_token)
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
