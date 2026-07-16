"""TikTok organic post prompt builder + generator tests."""

from unittest.mock import AsyncMock

import pytest

from peermarket_agent.claude import ClaudeResponse
from peermarket_agent.prompts.tiktok_post import (
    build_system_prompt,
    build_user_prompt,
    generate_tiktok_post,
)


def test_system_prompt_includes_brand_voice():
    brand_md = "# brand voice\n- Belgian dry humor\n- No em-dashes"
    sys = build_system_prompt(brand_md)
    assert "Belgian dry humor" in sys
    assert "TikTok organic" in sys
    assert "JSON" in sys


def test_user_prompt_specifies_language_and_theme():
    user = build_user_prompt(language="NL", theme="declutter")
    assert "NL" in user
    assert "declutter" in user


def test_user_prompt_supports_fr():
    user = build_user_prompt(language="FR", theme="vide-grenier")
    assert "FR" in user


async def test_generate_tiktok_post_parses_json_response():
    fake_client = AsyncMock()
    fake_client.complete = AsyncMock(
        return_value=ClaudeResponse(
            text='{"hook": "Wil je vandaag veilig en lokaal spullen verkopen?", "body": "Verkoop veilig op PeerMarket.", "cta": "Plaats het nu"}',
            input_tokens=200,
            output_tokens=40,
            model="claude-sonnet-4-6",
            stop_reason="end_turn",
        )
    )
    result = await generate_tiktok_post(
        claude=fake_client,
        brand_voice_md="# x",
        language="NL",
        theme="declutter",
    )
    assert result.hook == "Wil je vandaag veilig en lokaal spullen verkopen?"
    assert result.body == "Verkoop veilig op PeerMarket."
    assert result.cta == "Plaats het nu"
    assert result.cost_cents == 1


async def test_generate_tiktok_post_handles_malformed_json_raises_valueerror():
    fake_client = AsyncMock()
    fake_client.complete = AsyncMock(
        return_value=ClaudeResponse(
            text="not actually json",
            input_tokens=10,
            output_tokens=5,
            model="claude-sonnet-4-6",
            stop_reason="end_turn",
        )
    )
    with pytest.raises(ValueError, match="not valid JSON"):
        await generate_tiktok_post(
            claude=fake_client,
            brand_voice_md="# x",
            language="NL",
            theme="declutter",
        )
