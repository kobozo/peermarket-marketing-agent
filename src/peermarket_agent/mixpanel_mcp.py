"""Small async client for Mixpanel's hosted Streamable HTTP MCP server."""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx


class MixpanelMCPError(RuntimeError):
    pass


class MixpanelMCPClient:
    def __init__(self, username: str, secret: str, url: str) -> None:
        if not username or not secret or not url:
            raise ValueError("Mixpanel MCP credentials and URL are required")
        raw = f"{username}:{secret}".encode()
        self._auth = "Bearer Basic " + base64.b64encode(raw).decode()
        self._url = url

    async def _request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        headers = {
            "Authorization": self._auth,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        async with httpx.AsyncClient(timeout=30) as client:
            init = await client.post(
                self._url,
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "peermarket-jarvis", "version": "1"},
                    },
                },
            )
            init.raise_for_status()
            session = init.headers.get("mcp-session-id")
            if not session:
                raise MixpanelMCPError("Mixpanel MCP did not return a session id")
            headers["Mcp-Session-Id"] = session
            response = await client.post(
                self._url,
                headers=headers,
                json={"jsonrpc": "2.0", "id": 2, "method": method, "params": params or {}},
            )
            response.raise_for_status()
            payload = response.text
            if "data: " in payload:
                payload = payload.split("data: ", 1)[1].splitlines()[0]
            result = json.loads(payload)
            if "error" in result:
                raise MixpanelMCPError(result["error"].get("message", "MCP request failed"))
            return result.get("result")

    async def list_projects(self) -> Any:
        return await self._request("tools/call", {"name": "Get-Projects", "arguments": {}})

    async def business_context(self) -> Any:
        return await self._request("tools/call", {"name": "Get-Business-Context", "arguments": {}})
