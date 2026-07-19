"""Slack bridge — socket-mode listener + FastAPI healthcheck on :8090."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from functools import partial

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
from peermarket_agent.mixpanel_mcp import MixpanelMCPClient, MixpanelMCPError
from peermarket_agent.slack_bridge.ack_handler import handle_ack
from peermarket_agent.slack_bridge.ack_parser import parse_ack
from peermarket_agent.slack_bridge.revision_handler import handle_revision_reply
from peermarket_agent.slack_bridge.video_events import VideoUpload, extract_video_upload
from peermarket_agent.video_workflow import process_video_upload

log = structlog.get_logger(__name__)


_HELLO_TEXT = (
    "👋 PeerMarket marketing agent is online. I'm in Phase 0 — no posting yet, "
    "just confirming I can hear you."
)
_UNAUTHORIZED_TEXT = "This Slack user is not authorized to manage marketing drafts."


def is_authorized_user(user_id: str, founder_user_id: str | None = None) -> bool:
    founder = founder_user_id or os.getenv("SLACK_FOUNDER_USER_ID", "")
    allowed = {
        item.strip()
        for item in os.getenv("SLACK_AGENT_ALLOWED_USER_IDS", "").split(",")
        if item.strip()
    }
    # A Slack event is already authenticated by Slack. When no explicit
    # allowlist is configured, permit workspace members; deployments can opt
    # into a narrower list with SLACK_AGENT_ALLOWED_USER_IDS.
    return (
        (not founder and not allowed) or "*" in allowed or user_id == founder or user_id in allowed
    )


async def _chat_reply(text_msg: str, claude: ClaudeClient) -> str:
    try:
        response = await claude.complete(
            system=(
                "You are Jarvis, the PeerMarket marketing operations agent. "
                "Treat the user's message as feedback or a request for investigation. "
                "Reply in Dutch unless the user writes another language. State what "
                "you understood, propose concrete investigative steps, and mark any "
                "action needing explicit approval. Never claim a change happened unless executed."
            ),
            user=text_msg,
            temperature=0.2,
            max_tokens=800,
        )
        return response.text.strip() or "Ik heb je feedback ontvangen en maak een onderzoeksplan."
    except Exception:
        log.exception("slack_bridge.chat_reply_failed")
        return "👋 PeerMarket marketing agent is online, maar de analyse-service is tijdelijk niet beschikbaar."


async def _mixpanel_reply(text_msg: str) -> str:
    """Run a safe discovery check; detailed analysis remains conversational."""
    settings = get_settings()
    if not settings.mixpanel_mcp_username or not settings.mixpanel_mcp_secret:
        return "Mixpanel is nog niet geconfigureerd in Jarvis."
    client = MixpanelMCPClient(
        settings.mixpanel_mcp_username, settings.mixpanel_mcp_secret, settings.mixpanel_mcp_url
    )
    try:
        context = await client.business_context()
        projects = await client.list_projects()
        # Keep the Slack response compact and do not expose raw event/user data.
        project_text = json.dumps(projects, ensure_ascii=False)[:1200]
        context_text = json.dumps(context, ensure_ascii=False)[:1200]
        return (
            "📊 Mixpanel-verbinding werkt. Jarvis kan nu projecten, events, funnels en "
            "dashboards analyseren.\n\n"
            f"Projecten: {project_text}\nBusiness context: {context_text}"
        )
    except (MixpanelMCPError, Exception):
        log.exception("slack_bridge.mixpanel_failed")
        return "Mixpanel is geconfigureerd, maar de testquery faalde. Controleer projectrechten."


async def handle_app_mention(event: dict, say) -> None:
    if event.get("bot_id"):
        return
    if not is_authorized_user(event.get("user", "unknown")):
        await say(text=_UNAUTHORIZED_TEXT)
        return
    try:
        claude = ClaudeClient(get_settings().anthropic_api_key)
    except Exception:
        await say(text=_HELLO_TEXT)
        return
    await say(text=await _chat_reply(event.get("text") or "", claude))


async def handle_im(
    event: dict,
    say,
    body: dict | None = None,
    *,
    founder_user_id: str,
    claude: ClaudeClient | None = None,
) -> None:
    upload = extract_video_upload(event)
    if upload is not None:
        asyncio.create_task(_route_video_upload(get_engine(), upload))
        return
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
    if not is_authorized_user(user_id, founder_user_id):
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
    if text_msg.strip().lower().startswith(("mixpanel", "/mixpanel")):
        await say(text=await _mixpanel_reply(text_msg))
        return
    if claude is None:
        try:
            claude = ClaudeClient(get_settings().anthropic_api_key)
        except Exception:
            await say(text=_HELLO_TEXT)
            return
    await say(text=await _chat_reply(text_msg, claude))


async def _route_video_upload(engine, upload: VideoUpload) -> None:
    settings = get_settings()
    result = await process_video_upload(
        engine,
        AsyncWebClient(token=settings.slack_bot_token),
        ClaudeClient(api_key=settings.anthropic_api_key),
        upload,
        settings,
    )
    log.info("slack_video.processed", file_id=upload.file_id, status=result.status)


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
