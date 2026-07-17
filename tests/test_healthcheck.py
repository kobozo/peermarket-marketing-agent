"""Healthcheck endpoint tests."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

from peermarket_agent.slack_bridge.app import build_healthz_api


async def test_healthz_returns_ok():
    transport = httpx.ASGITransport(app=build_healthz_api())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/agent/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


async def test_healthz_remains_available_while_media_task_runs(monkeypatch):
    from peermarket_agent.slack_bridge import app as bridge_app

    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked_route(*_args):
        started.set()
        await release.wait()

    monkeypatch.setattr(bridge_app, "get_engine", lambda: object())
    monkeypatch.setattr(
        bridge_app,
        "get_settings",
        lambda: SimpleNamespace(slack_founder_user_id="U123"),
    )
    monkeypatch.setattr(bridge_app, "_route_video_upload", blocked_route)
    event = {
        "channel": "C123",
        "channel_type": "channel",
        "thread_ts": "1710000000.123456",
        "user": "U123",
        "files": [{"id": "F123", "name": "recording.mp4", "mimetype": "video/mp4"}],
    }

    await bridge_app.handle_im(event=event, say=AsyncMock(), founder_user_id="U123")
    await started.wait()
    transport = httpx.ASGITransport(app=build_healthz_api())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/agent/healthz")
    release.set()

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
