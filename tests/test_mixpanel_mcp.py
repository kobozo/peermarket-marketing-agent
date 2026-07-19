import json

import httpx
import pytest
import respx

from peermarket_agent.mixpanel_mcp import MixpanelMCPClient


@pytest.mark.asyncio
@respx.mock
async def test_mixpanel_mcp_discovers_business_context_and_projects():
    route = respx.post("https://mcp-eu.mixpanel.com/mcp").mock(
        side_effect=[
            httpx.Response(
                200,
                headers={"mcp-session-id": "s1"},
                text='event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{}}\n\n',
            ),
            httpx.Response(
                200,
                text="event: message\ndata: "
                + json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"ok": True}})
                + "\n\n",
            ),
            httpx.Response(
                200,
                headers={"mcp-session-id": "s2"},
                text='event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{}}\n\n',
            ),
            httpx.Response(
                200,
                text="event: message\ndata: "
                + json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"projects": []}})
                + "\n\n",
            ),
        ]
    )
    client = MixpanelMCPClient("user", "secret", "https://mcp-eu.mixpanel.com/mcp")
    assert await client.business_context() == {"ok": True}
    assert await client.list_projects() == {"projects": []}
    assert route.call_count == 4
    assert "Bearer Basic" in route.calls[0].request.headers["Authorization"]
