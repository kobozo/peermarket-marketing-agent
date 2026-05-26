"""SEO PR meta-tag generator tests."""

from unittest.mock import AsyncMock

import pytest

from peermarket_agent.claude import ClaudeResponse
from peermarket_agent.prompts.seo_pr import generate_seo_meta


async def test_generate_seo_meta_for_about_page_nl():
    fake = AsyncMock()
    fake.complete = AsyncMock(
        return_value=ClaudeResponse(
            text=(
                '{"title": "Veilig tweedehands kopen en verkopen | PeerMarket",'
                ' "description": "Belgische marktplaats met geverifieerde verkopers. '
                'Plaats je eerste item gratis."}'
            ),
            input_tokens=200,
            output_tokens=40,
            model="claude-sonnet-4-6",
            stop_reason="end_turn",
        )
    )
    result = await generate_seo_meta(
        claude=fake,
        brand_voice_md="# x",
        language="NL",
        page_path="/about",
        page_subject="who we are, why we verify identities",
    )
    assert "PeerMarket" in result.title
    assert len(result.title) <= 60
    assert 50 <= len(result.description) <= 160


async def test_generate_seo_meta_rejects_too_long_title():
    fake = AsyncMock()
    fake.complete = AsyncMock(
        return_value=ClaudeResponse(
            text='{"title": "' + "x" * 80 + '", "description": "ok ' + "y" * 100 + '"}',
            input_tokens=10,
            output_tokens=10,
            model="claude-sonnet-4-6",
            stop_reason="end_turn",
        )
    )
    with pytest.raises(ValueError, match="title"):
        await generate_seo_meta(
            claude=fake,
            brand_voice_md="# x",
            language="NL",
            page_path="/about",
            page_subject="x",
        )


async def test_generate_seo_meta_rejects_too_short_description():
    fake = AsyncMock()
    fake.complete = AsyncMock(
        return_value=ClaudeResponse(
            text='{"title": "ok title", "description": "too short"}',
            input_tokens=10,
            output_tokens=10,
            model="claude-sonnet-4-6",
            stop_reason="end_turn",
        )
    )
    with pytest.raises(ValueError, match="description"):
        await generate_seo_meta(
            claude=fake,
            brand_voice_md="# x",
            language="NL",
            page_path="/about",
            page_subject="x",
        )
