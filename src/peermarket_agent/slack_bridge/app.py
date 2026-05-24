"""Slack bridge — socket-mode listener + FastAPI healthcheck on :8090."""
from __future__ import annotations

import asyncio
import contextlib

import click
import structlog
from fastapi import FastAPI
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from uvicorn import Config, Server

from peermarket_agent.config import get_settings

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
    if event.get("channel_type") != "im":
        return
    await say(text=_HELLO_TEXT)


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
