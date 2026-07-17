"""Claude async wrapper tests — no real API calls."""

import base64
from unittest.mock import AsyncMock

import pytest

from peermarket_agent.claude import ClaudeClient, ClaudeResponse


@pytest.fixture
def fake_anthropic_client(monkeypatch):
    fake = AsyncMock()
    fake.messages.create = AsyncMock(
        return_value=AsyncMock(
            content=[AsyncMock(type="text", text="hello world")],
            usage=AsyncMock(input_tokens=10, output_tokens=2),
            model="claude-sonnet-4-6",
            stop_reason="end_turn",
        )
    )
    monkeypatch.setattr(
        "peermarket_agent.claude.AsyncAnthropic",
        lambda *a, **kw: fake,
    )
    return fake


async def test_complete_returns_text_and_usage(fake_anthropic_client):
    client = ClaudeClient(api_key="sk-ant-test")
    resp = await client.complete(
        system="You are a brand voice tester.",
        user="Say hello",
        model="claude-sonnet-4-6",
        max_tokens=100,
    )
    assert isinstance(resp, ClaudeResponse)
    assert resp.text == "hello world"
    assert resp.input_tokens == 10
    assert resp.output_tokens == 2
    assert resp.model == "claude-sonnet-4-6"


async def test_complete_uses_default_model(fake_anthropic_client):
    client = ClaudeClient(api_key="sk-ant-test")
    await client.complete(system="x", user="y")
    args, kwargs = fake_anthropic_client.messages.create.await_args
    assert kwargs["model"] == "claude-sonnet-4-6"


async def test_complete_passes_temperature_and_max_tokens(fake_anthropic_client):
    client = ClaudeClient(api_key="sk-ant-test")
    await client.complete(system="x", user="y", temperature=0.3, max_tokens=500)
    args, kwargs = fake_anthropic_client.messages.create.await_args
    assert kwargs["temperature"] == 0.3
    assert kwargs["max_tokens"] == 500


async def test_complete_strips_text_blocks(fake_anthropic_client):
    """Anthropic returns content as a list of blocks; we want plain text."""
    fake_anthropic_client.messages.create = AsyncMock(
        return_value=AsyncMock(
            content=[
                AsyncMock(type="text", text="line one\n"),
                AsyncMock(type="text", text="line two"),
            ],
            usage=AsyncMock(input_tokens=1, output_tokens=1),
            model="claude-sonnet-4-6",
            stop_reason="end_turn",
        )
    )
    client = ClaudeClient(api_key="sk-ant-test")
    resp = await client.complete(system="x", user="y")
    assert resp.text == "line one\nline two"


async def test_complete_with_images_sends_ordered_jpeg_blocks(fake_anthropic_client, tmp_path):
    first = tmp_path / "first.jpg"
    second = tmp_path / "second.jpeg"
    first.write_bytes(b"first jpeg")
    second.write_bytes(b"second jpeg")
    client = ClaudeClient(api_key="sk-ant-test")

    response = await client.complete_with_images(
        system="Review the recording.",
        user="Approved script: hello",
        images=[first, second],
        temperature=0.0,
        max_tokens=400,
    )

    assert response.text == "hello world"
    _args, kwargs = fake_anthropic_client.messages.create.await_args
    assert kwargs["messages"] == [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": base64.b64encode(b"first jpeg").decode("ascii"),
                    },
                },
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": base64.b64encode(b"second jpeg").decode("ascii"),
                    },
                },
                {"type": "text", "text": "Approved script: hello"},
            ],
        }
    ]
