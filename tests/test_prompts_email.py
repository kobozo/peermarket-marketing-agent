"""Email re-engagement prompt builder + generator tests."""

from unittest.mock import AsyncMock

import pytest

from peermarket_agent.claude import ClaudeResponse
from peermarket_agent.prompts.email_re_engagement import (
    build_system_prompt,
    generate_email,
)


def test_system_prompt_mentions_email_constraints():
    sys = build_system_prompt("# voice")
    assert "subject" in sys.lower()
    assert "Reply-To" not in sys
    assert "JSON" in sys


async def test_generate_email_parses_subject_and_body():
    fake = AsyncMock()
    fake.complete = AsyncMock(
        return_value=ClaudeResponse(
            text='{"subject": "Je hebt nog niets verkocht", "body": "Hoi, je hebt een account..."}',
            input_tokens=250,
            output_tokens=80,
            model="claude-sonnet-4-6",
            stop_reason="end_turn",
        )
    )
    result = await generate_email(
        claude=fake,
        brand_voice_md="# x",
        language="NL",
        audience="dormant_signups",
    )
    assert result.subject == "Je hebt nog niets verkocht"
    assert result.body.startswith("Hoi")
    assert result.cost_cents >= 1


async def test_generate_email_validates_subject_length_under_60_chars():
    fake = AsyncMock()
    fake.complete = AsyncMock(
        return_value=ClaudeResponse(
            text='{"subject": "' + "x" * 80 + '", "body": "ok"}',
            input_tokens=10,
            output_tokens=10,
            model="claude-sonnet-4-6",
            stop_reason="end_turn",
        )
    )
    with pytest.raises(ValueError, match="subject"):
        await generate_email(
            claude=fake,
            brand_voice_md="# x",
            language="NL",
            audience="dormant_signups",
        )
