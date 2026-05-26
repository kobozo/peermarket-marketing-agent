"""Brand-quality gate tests — score a draft against brand voice."""

from unittest.mock import AsyncMock

import pytest

from peermarket_agent.brand_quality import score_draft
from peermarket_agent.claude import ClaudeResponse


async def test_score_draft_extracts_integer_from_response():
    fake = AsyncMock()
    fake.complete = AsyncMock(
        return_value=ClaudeResponse(
            text='{"score": 87, "notes": "Strong NL voice, on-brand."}',
            input_tokens=300,
            output_tokens=20,
            model="claude-sonnet-4-6",
            stop_reason="end_turn",
        )
    )
    score, notes = await score_draft(
        claude=fake,
        brand_voice_md="# x",
        copy="Marktplaats moe? Verkoop veilig op PeerMarket.",
    )
    assert score == 87
    assert notes == "Strong NL voice, on-brand."


async def test_score_draft_clamps_to_0_100():
    fake = AsyncMock()
    fake.complete = AsyncMock(
        return_value=ClaudeResponse(
            text='{"score": 150, "notes": "x"}',
            input_tokens=10,
            output_tokens=5,
            model="claude-sonnet-4-6",
            stop_reason="end_turn",
        )
    )
    score, _ = await score_draft(claude=fake, brand_voice_md="x", copy="x")
    assert score == 100


async def test_score_draft_clamps_negative():
    fake = AsyncMock()
    fake.complete = AsyncMock(
        return_value=ClaudeResponse(
            text='{"score": -10, "notes": "x"}',
            input_tokens=10,
            output_tokens=5,
            model="claude-sonnet-4-6",
            stop_reason="end_turn",
        )
    )
    score, _ = await score_draft(claude=fake, brand_voice_md="x", copy="x")
    assert score == 0


async def test_score_draft_handles_malformed_response():
    fake = AsyncMock()
    fake.complete = AsyncMock(
        return_value=ClaudeResponse(
            text="not json",
            input_tokens=10,
            output_tokens=5,
            model="claude-sonnet-4-6",
            stop_reason="end_turn",
        )
    )
    with pytest.raises(ValueError, match="not valid JSON"):
        await score_draft(claude=fake, brand_voice_md="x", copy="x")
