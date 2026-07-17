"""Meta ad creative generator tests."""

import random
from unittest.mock import AsyncMock

import pytest

from peermarket_agent.claude import ClaudeResponse
from peermarket_agent.prompts.meta_ad_creative import (
    AUDIENCE_PROFILES,
    generate_meta_ad_creative,
    pick_audience,
)

_GOOD_PAYLOAD = (
    "{"
    '"primary_text": "Marktplaats moe? Op PeerMarket is elke verkoper geverifieerd via Stripe Identity. '
    'Geen lokvogels, geen stress. Begin gratis met je eerste plaatsing en zie zelf het verschil.",'
    '"headline": "Veilig tweedehands verkopen",'
    '"description": "Geverifieerde verkopers",'
    '"cta_label": "Learn More",'
    '"suggested_daily_budget_eur": 10'
    "}"
)


def _resp(text: str) -> ClaudeResponse:
    return ClaudeResponse(
        text=text,
        input_tokens=400,
        output_tokens=80,
        model="claude-sonnet-4-6",
        stop_reason="end_turn",
    )


def test_pick_audience_returns_valid_key():
    key = pick_audience(random.Random(42))
    assert key in AUDIENCE_PROFILES


def test_audience_profiles_include_both_personas():
    assert "declutterers" in AUDIENCE_PROFILES
    assert "trust_conscious_locals" in AUDIENCE_PROFILES


async def test_generate_meta_ad_creative_happy_path():
    fake = AsyncMock()
    fake.complete = AsyncMock(return_value=_resp(_GOOD_PAYLOAD))
    result = await generate_meta_ad_creative(
        claude=fake,
        brand_voice_md="# x",
        language="NL",
        audience_profile_key="declutterers",
    )
    assert result.cta_label == "Learn More"
    assert result.suggested_daily_budget_eur == 10
    assert result.audience_profile_key == "declutterers"
    assert 125 <= len(result.primary_text) <= 300
    assert len(result.headline) <= 40
    assert len(result.description) <= 40
    assert result.cost_cents >= 1


async def test_generate_meta_ad_creative_rejects_unknown_audience():
    fake = AsyncMock()
    with pytest.raises(ValueError, match="unknown audience profile"):
        await generate_meta_ad_creative(
            claude=fake,
            brand_voice_md="# x",
            language="NL",
            audience_profile_key="not_a_real_audience",
        )


async def test_generate_meta_ad_creative_rejects_long_primary_text():
    bad_payload = (
        "{"
        f'"primary_text": "{"x" * 400}",'
        '"headline": "ok",'
        '"description": "ok",'
        '"cta_label": "Learn More",'
        '"suggested_daily_budget_eur": 10'
        "}"
    )
    fake = AsyncMock()
    fake.complete = AsyncMock(return_value=_resp(bad_payload))
    with pytest.raises(ValueError, match="primary_text"):
        await generate_meta_ad_creative(
            claude=fake,
            brand_voice_md="# x",
            language="NL",
            audience_profile_key="declutterers",
        )


async def test_generate_meta_ad_creative_rejects_short_primary_text():
    bad_payload = (
        "{"
        '"primary_text": "too short",'
        '"headline": "ok",'
        '"description": "ok",'
        '"cta_label": "Learn More",'
        '"suggested_daily_budget_eur": 10'
        "}"
    )
    fake = AsyncMock()
    fake.complete = AsyncMock(return_value=_resp(bad_payload))
    with pytest.raises(ValueError, match="primary_text"):
        await generate_meta_ad_creative(
            claude=fake,
            brand_voice_md="# x",
            language="NL",
            audience_profile_key="declutterers",
        )


async def test_generate_meta_ad_creative_rejects_disallowed_cta():
    bad_payload = (
        "{"
        f'"primary_text": "{"x" * 150}",'
        '"headline": "ok",'
        '"description": "ok",'
        '"cta_label": "Click Here Now",'
        '"suggested_daily_budget_eur": 10'
        "}"
    )
    fake = AsyncMock()
    fake.complete = AsyncMock(return_value=_resp(bad_payload))
    with pytest.raises(ValueError, match="cta_label"):
        await generate_meta_ad_creative(
            claude=fake,
            brand_voice_md="# x",
            language="NL",
            audience_profile_key="declutterers",
        )


async def test_generate_meta_ad_creative_rejects_budget_out_of_range():
    bad_payload = (
        "{"
        f'"primary_text": "{"x" * 150}",'
        '"headline": "ok",'
        '"description": "ok",'
        '"cta_label": "Learn More",'
        '"suggested_daily_budget_eur": 100'
        "}"
    )
    fake = AsyncMock()
    fake.complete = AsyncMock(return_value=_resp(bad_payload))
    with pytest.raises(ValueError, match="suggested_daily_budget_eur"):
        await generate_meta_ad_creative(
            claude=fake,
            brand_voice_md="# x",
            language="NL",
            audience_profile_key="declutterers",
        )


def test_autonomous_replacement_prompt_requires_native_strict_locale_json():
    from peermarket_agent.prompts.meta_ad_creative import build_replacement_user_prompt

    prompt = build_replacement_user_prompt(
        locale="FR", changed_dimension="copy", source={"headline": "Exact"}, learnings=("one",) * 7
    )
    assert "Locale: FR" in prompt
    assert "written natively" in prompt
    assert "strict JSON" in prompt
    assert prompt.count("- one") == 5
