"""Controlled autonomous replacement generation contracts."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from peermarket_agent.autonomy.contracts import DecisionKind, FrozenDecision
from peermarket_agent.autonomy.replacements import (
    LocaleCreative,
    ReplacementSource,
    build_replacement,
)
from peermarket_agent.claude import ClaudeResponse


@pytest.fixture(autouse=True)
def _passing_brand_gate(monkeypatch):
    monkeypatch.setattr(
        "peermarket_agent.autonomy.replacements.score_draft",
        AsyncMock(return_value=(91, "ok")),
    )


def _locale(locale: str, hook: str, body: str) -> LocaleCreative:
    words = {
        "NL": ("Veilig lokaal kopen", "Vertrouwde buren"),
        "FR": ("Achetez localement", "Des voisins fiables"),
        "EN": ("Buy safely nearby", "Trusted local people"),
    }[locale]
    return LocaleCreative(locale, hook, body, words[0], words[1], "Learn More")


def _source(dimension: str) -> ReplacementSource:
    return ReplacementSource(
        draft_id=42,
        campaign_id="123",
        experiment_id="experiment-1",
        changed_dimension=dimension,
        locales={
            "NL": _locale(
                "NL",
                "Koop veilig dichtbij",
                "Ontdek de marktplaats voor geverifieerde mensen in jouw buurt.",
            ),
            "FR": _locale(
                "FR",
                "Achetez en sécurité",
                "Découvrez la place de marché des personnes vérifiées près de chez vous.",
            ),
            "EN": _locale(
                "EN",
                "Buy safely nearby",
                "Discover the marketplace for verified people in your neighbourhood.",
            ),
        },
        audience_profile_key="declutterers",
        image_prompt="A bright Belgian neighbourhood market",
        asset_path="assets/source.png",
        daily_budget_eur=10,
        landing_page_url="https://www.peermarket.eu/path?keep=1#faq",
    )


def _decision(source: ReplacementSource) -> FrozenDecision:
    return FrozenDecision(
        kind=DecisionKind.REPLACE,
        campaign_id=source.campaign_id,
        evidence=source.frozen_evidence(),
        reason="losing comparable variant",
        window_start=datetime(2026, 7, 16, tzinfo=UTC),
        window_end=datetime(2026, 7, 17, tzinfo=UTC),
        idempotency_key="replace-1",
    )


def _response(source: ReplacementSource, locale: str, **changes: str) -> ClaudeResponse:
    item = source.locales[locale]
    payload = {
        "locale": locale,
        "changed_dimension": source.changed_dimension,
        "hook": item.hook,
        "body": item.body,
        "headline": item.headline,
        "description": item.description,
        "cta_label": item.cta_label,
        "audience_profile_key": source.audience_profile_key,
        "image_prompt": source.image_prompt,
        "asset_path": source.asset_path,
        "suggested_daily_budget_eur": source.daily_budget_eur,
    }
    payload.update(changes)
    if source.changed_dimension == "copy" and "body" in changes:
        payload.update(
            {
                "headline": {
                    "NL": "Verkoop veilig",
                    "FR": "Vendez sereinement",
                    "EN": "Sell with confidence",
                }[locale],
                "description": {
                    "NL": "Echte mensen dichtbij",
                    "FR": "Des personnes vérifiées",
                    "EN": "Verified people nearby",
                }[locale],
                "cta_label": "Get Started",
            }
        )
    import json

    return ClaudeResponse(json.dumps(payload), 10, 20, "test", "end_turn")


def _quality(locale: str, **changes) -> ClaudeResponse:
    import json

    payload = {
        "locale": locale,
        "exact_language": True,
        "idiomatic": True,
        "literal_translation": False,
        "field_results": {
            "hook": True,
            "body": True,
            "headline": True,
            "description": True,
            "cta_label": True,
        },
        "evidence": "Native and idiomatic complete creative.",
    }
    payload.update(changes)
    return ClaudeResponse(json.dumps(payload), 5, 5, "test", "end_turn")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("dimension", "changes"),
    [
        (
            "hook",
            {
                "hook": {
                    "NL": "Verkoop veilig vandaag",
                    "FR": "Vendez en sécurité",
                    "EN": "Sell safely today",
                }
            },
        ),
        (
            "copy",
            {
                "body": {
                    "NL": "Vind geverifieerde kopers in je buurt en verkoop met vertrouwen.",
                    "FR": "Trouvez des acheteurs vérifiés près de chez vous et vendez en confiance.",
                    "EN": "Find verified buyers nearby and sell with confidence today.",
                }
            },
        ),
        (
            "visual",
            {
                "image_prompt": {
                    locale: "Belgian neighbours at a local market" for locale in ("NL", "FR", "EN")
                }
            },
        ),
        (
            "audience",
            {
                "audience_profile_key": {
                    "NL": "trust_conscious_locals",
                    "FR": "trust_conscious_locals",
                    "EN": "trust_conscious_locals",
                }
            },
        ),
    ],
)
async def test_each_dimension_changes_only_its_implementable_fields(dimension, changes):
    source = _source(dimension)
    fake = AsyncMock()
    fake.complete.side_effect = [
        response
        for locale in ("NL", "FR", "EN")
        for response in (
            _response(source, locale, **{field: values[locale] for field, values in changes.items()}),
            _quality(locale),
        )
    ]
    draft = await build_replacement(
        AsyncMock(), fake, source, _decision(source), persist=lambda _: 99
    )
    assert set(draft.locales) == {"NL", "FR", "EN"}
    assert draft.changed_dimension == dimension
    assert draft.landing_page_url == (
        "https://www.peermarket.eu/path?keep=1&utm_source=facebook&utm_medium=paid_social"
        "&utm_campaign=peermarket&utm_content=draft-99#faq"
    )


@pytest.mark.asyncio
async def test_complete_locales_are_native_and_not_dutch_copies():
    source = _source("copy")
    fake = AsyncMock()
    fake.complete.side_effect = [
        _response(
            source, "NL", body="Vind geverifieerde kopers in je buurt en verkoop met vertrouwen."
        ),
        _quality("NL"),
        _response(
            source,
            "FR",
            body="Trouvez des acheteurs vérifiés près de chez vous et vendez en confiance.",
        ),
        _quality("FR"),
        _response(source, "EN", body="Find verified buyers nearby and sell with confidence today."),
        _quality("EN"),
    ]
    draft = await build_replacement(
        AsyncMock(), fake, source, _decision(source), persist=lambda _: 99
    )
    assert draft.locales["FR"].body != draft.locales["NL"].body
    assert draft.locales["EN"].headline != draft.locales["NL"].headline
    assert all(draft.brand_scores[locale] == 91 for locale in ("NL", "FR", "EN"))
    assert set(draft.locale_quality or {}) == {"NL", "FR", "EN"}


@pytest.mark.asyncio
async def test_native_reviewer_rejects_literal_translation_flag():
    source = _source("copy")
    fake = AsyncMock()
    fake.complete.side_effect = [
        _response(source, "NL", body="Vind geverifieerde kopers in je buurt en verkoop met vertrouwen."),
        _quality("NL", literal_translation=True),
    ]
    with pytest.raises(ValueError, match="native-quality"):
        await build_replacement(AsyncMock(), fake, source, _decision(source), persist=lambda _: 99)


@pytest.mark.asyncio
async def test_rejects_literal_translation_or_wrong_language_before_persistence():
    source = _source("copy")
    dutch = "Vind geverifieerde kopers in je buurt en verkoop met vertrouwen."
    fake = AsyncMock()
    fake.complete.side_effect = [
        _response(source, "NL", body=dutch),
        _quality("NL"),
        _response(source, "FR", body=dutch),
    ]
    persisted = AsyncMock()
    with pytest.raises(ValueError, match="native|translation|language"):
        await build_replacement(AsyncMock(), fake, source, _decision(source), persist=persisted)
    persisted.assert_not_awaited()


@pytest.mark.asyncio
async def test_url_rejects_non_peermarket_destination():
    source = _source("copy")
    object.__setattr__(source, "landing_page_url", "https://evil.example/path")
    with pytest.raises(ValueError, match="HTTPS PeerMarket"):
        await build_replacement(AsyncMock(), AsyncMock(), source, _decision(source))


def test_dimension_needing_missing_baseline_fails_closed():
    source = _source("visual")
    object.__setattr__(source, "asset_path", "")
    with pytest.raises(ValueError, match="visual baseline"):
        source.validate_baseline()
