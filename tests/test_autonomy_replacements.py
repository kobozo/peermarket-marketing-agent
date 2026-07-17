"""Controlled autonomous replacement generation contracts."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from peermarket_agent.autonomy.contracts import DecisionKind, FrozenDecision
from peermarket_agent.autonomy.replacements import ReplacementSource, build_replacement
from peermarket_agent.claude import ClaudeResponse


def _response(language: str, primary: str) -> ClaudeResponse:
    primary = (primary + " ") * 2
    return ClaudeResponse(
        text=(
            '{"locale":"' + language + '","changed_dimension":"copy",'
            '"primary_text":"' + primary + '","headline":"Veilig lokaal kopen",'
            '"description":"Geverifieerde mensen dichtbij","cta_label":"Learn More",'
            '"audience_profile_key":"declutterers","suggested_daily_budget_eur":10}'
        ),
        input_tokens=10,
        output_tokens=20,
        model="test",
        stop_reason="end_turn",
    )


@pytest.mark.asyncio
async def test_autonomous_replacement_authors_exact_locales_and_preserves_frozen_fields():
    source = ReplacementSource(
        draft_id=42,
        campaign_id="123",
        experiment_id="experiment-1",
        changed_dimension="copy",
        audience_profile_key="declutterers",
        headline="Veilig lokaal kopen",
        description="Geverifieerde mensen dichtbij",
        cta_label="Learn More",
        primary_text="source copy",
        daily_budget_eur=10,
        landing_page_url="https://peermarket.eu/",
    )
    evidence = source.frozen_evidence()
    decision = FrozenDecision(
        kind=DecisionKind.REPLACE,
        campaign_id="123",
        evidence=evidence,
        reason="losing comparable variant",
        window_start=datetime(2026, 7, 16, tzinfo=UTC),
        window_end=datetime(2026, 7, 17, tzinfo=UTC),
        idempotency_key="replace-1",
    )
    fake = AsyncMock()
    fake.complete = AsyncMock(
        side_effect=[
            _response(
                "NL",
                "Nederlandse tekst over veilig tweedehands handelen met echte mensen dichtbij.",
            ),
            _response(
                "FR", "Texte français sur les achats de seconde main sûrs entre voisins vérifiés."
            ),
            _response(
                "EN", "English copy about safe secondhand trading with verified people nearby."
            ),
        ]
    )
    engine = AsyncMock()

    draft = await build_replacement(engine, fake, source, decision, persist=lambda _: 99)

    assert set(draft.locales) == {"NL", "FR", "EN"}
    assert draft.changed_dimension == "copy"
    assert draft.source_draft_id == 42 and draft.experiment_id == "experiment-1"
    assert draft.daily_budget_eur == 10
    assert draft.landing_page_url.endswith("utm_content=draft-99")
    assert len(fake.complete.await_args_list) == 3
    for locale, call in zip(("NL", "FR", "EN"), fake.complete.await_args_list, strict=True):
        assert f"Locale: {locale}" in call.kwargs["user"]


@pytest.mark.asyncio
async def test_autonomous_replacement_refuses_changed_frozen_source():
    source = ReplacementSource(
        draft_id=42,
        campaign_id="123",
        experiment_id="e",
        changed_dimension="copy",
        audience_profile_key="declutterers",
        headline="h",
        description="d",
        cta_label="Learn More",
        primary_text="copy",
        daily_budget_eur=10,
        landing_page_url="https://peermarket.eu/",
    )
    evidence = source.frozen_evidence()
    evidence["source"]["headline"] = "different"
    decision = FrozenDecision(
        kind=DecisionKind.REPLACE,
        campaign_id="123",
        evidence=evidence,
        reason="replace",
        window_start=datetime(2026, 7, 16, tzinfo=UTC),
        window_end=datetime(2026, 7, 17, tzinfo=UTC),
        idempotency_key="replace-2",
    )
    with pytest.raises(ValueError, match="frozen"):
        await build_replacement(AsyncMock(), AsyncMock(), source, decision)
