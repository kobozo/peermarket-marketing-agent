"""Schema-first multilingual creative replacements for autonomous Meta experiments."""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, replace
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from peermarket_agent._json_parse import parse_claude_json
from peermarket_agent.action_contracts import validate_meta
from peermarket_agent.agent.cli_draft import recent_relevant_learnings
from peermarket_agent.autonomy.contracts import DecisionKind, FrozenDecision
from peermarket_agent.brand_quality import BRAND_SCORE_THRESHOLD, score_draft
from peermarket_agent.claude import ClaudeClient
from peermarket_agent.prompts.brand_voice import load_brand_voice
from peermarket_agent.prompts.meta_ad_creative import (
    AUDIENCE_PROFILES,
    _cost_cents,
    build_replacement_user_prompt,
    build_system_prompt,
)

LOCALES = ("NL", "FR", "EN")
DIMENSIONS = {"hook", "copy", "visual", "audience"}
_TEXT_FIELDS = ("primary_text", "headline", "description", "cta_label")
_LOCALE_MARKER = re.compile(r"(^|\s)(?:\[(?:NL|FR|EN)\]|(?:NL|FR|EN):)(?:\s|$)", re.I)


@dataclass(frozen=True, slots=True)
class ReplacementSource:
    draft_id: int
    campaign_id: str
    experiment_id: str
    changed_dimension: str
    audience_profile_key: str
    headline: str
    description: str
    cta_label: str
    primary_text: str
    daily_budget_eur: int
    landing_page_url: str

    def frozen_evidence(self) -> dict:
        return {"source": asdict(self)}


@dataclass(frozen=True, slots=True)
class ReplacementLocale:
    locale: str
    primary_text: str
    headline: str
    description: str
    cta_label: str
    audience_profile_key: str


@dataclass(frozen=True, slots=True)
class ReplacementDraft:
    id: int
    locales: Mapping[str, ReplacementLocale]
    changed_dimension: str
    source_draft_id: int
    experiment_id: str
    daily_budget_eur: int
    landing_page_url: str
    cost_cents: int
    brand_scores: Mapping[str, int]


def _source_payload(source: ReplacementSource) -> dict:
    return {
        "primary_text": source.primary_text,
        "headline": source.headline,
        "description": source.description,
        "cta_label": source.cta_label,
        "audience_profile_key": source.audience_profile_key,
        "suggested_daily_budget_eur": source.daily_budget_eur,
    }


def _verify_frozen(source: ReplacementSource, decision: FrozenDecision) -> None:
    if decision.kind is not DecisionKind.REPLACE or decision.campaign_id != source.campaign_id:
        raise ValueError("replacement does not match frozen decision")
    if source.changed_dimension not in DIMENSIONS:
        raise ValueError("invalid frozen replacement dimension")
    frozen = decision.evidence.get("source")
    if frozen != asdict(source):
        raise ValueError("source metadata differs from frozen decision evidence")
    if type(source.daily_budget_eur) is not int or not 5 <= source.daily_budget_eur <= 20:
        raise ValueError("frozen daily budget must be an integer from 5-20")


def _validate_locale(payload: dict, source: ReplacementSource, locale: str) -> ReplacementLocale:
    expected = {
        "locale",
        "changed_dimension",
        "primary_text",
        "headline",
        "description",
        "cta_label",
        "audience_profile_key",
        "suggested_daily_budget_eur",
    }
    if set(payload) != expected:
        raise ValueError("replacement JSON schema has missing or extra fields")
    if payload["locale"] != locale or payload["changed_dimension"] != source.changed_dimension:
        raise ValueError("replacement locale or dimension differs from request")
    validate_meta(payload, allowed_audiences=set(AUDIENCE_PROFILES))
    if payload["suggested_daily_budget_eur"] != source.daily_budget_eur:
        raise ValueError("replacement changed frozen budget")
    mutable = {
        "copy": {"primary_text"},
        "hook": {"primary_text"},
        "visual": set(),
        "audience": {"audience_profile_key"},
    }[source.changed_dimension]
    original = _source_payload(source)
    for field in set(original) - mutable - {"suggested_daily_budget_eur"}:
        if payload[field] != original[field]:
            raise ValueError(f"replacement changed frozen {field}")
    if not any(payload[field] != original[field] for field in mutable):
        raise ValueError("replacement did not change its primary dimension")
    if any(_LOCALE_MARKER.search(payload[field]) for field in _TEXT_FIELDS):
        raise ValueError("replacement contains a literal locale marker")
    return ReplacementLocale(
        locale=locale,
        primary_text=payload["primary_text"],
        headline=payload["headline"],
        description=payload["description"],
        cta_label=payload["cta_label"],
        audience_profile_key=payload["audience_profile_key"],
    )


def _campaign_url(url: str, draft_id: int) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update(
        utm_source="facebook",
        utm_medium="paid_social",
        utm_campaign="peermarket",
        utm_content=f"draft-{draft_id}",
    )
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


async def _persist(engine: AsyncEngine, draft: ReplacementDraft) -> int:
    metadata = {
        "autonomous_replacement": True,
        "source_draft_id": draft.source_draft_id,
        "experiment_id": draft.experiment_id,
        "changed_dimension": draft.changed_dimension,
        "locales": {key: asdict(value) for key, value in draft.locales.items()},
        "suggested_daily_budget_eur": draft.daily_budget_eur,
    }
    async with engine.begin() as conn:
        action_id = await conn.scalar(
            text("SELECT id FROM action_types WHERE name='meta_ad_creative'")
        )
        if action_id is None:
            raise ValueError("meta_ad_creative action type is not seeded")
        draft_id = await conn.scalar(
            text(
                "INSERT INTO drafts (action_type_id, channel, language, copy, asset_path, "
                "generation_cost_cents, brand_score, visual_truthfulness_pass, metadata) "
                "VALUES (:action, 'meta', 'EN', :copy, NULL, :cost, :score, TRUE, "
                "CAST(:metadata AS JSONB)) RETURNING id"
            ),
            {
                "action": action_id,
                "copy": "\n\n".join(v.primary_text for v in draft.locales.values()),
                "cost": draft.cost_cents,
                "score": min(draft.brand_scores.values()),
                "metadata": json.dumps(metadata),
            },
        )
    return int(draft_id)


async def _freeze_destination(engine: AsyncEngine, draft_id: int, url: str) -> None:
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "UPDATE drafts SET metadata=metadata || jsonb_build_object('landing_page_url', :url) "
                "WHERE id=:id AND metadata->>'autonomous_replacement'='true'"
            ),
            {"id": draft_id, "url": url},
        )
        if result.rowcount != 1:
            raise ValueError("persisted autonomous replacement draft disappeared")


async def build_replacement(
    engine: AsyncEngine,
    claude: ClaudeClient,
    source: ReplacementSource,
    decision: FrozenDecision,
    *,
    persist: Callable[[ReplacementDraft], int | Awaitable[int]] | None = None,
) -> ReplacementDraft:
    """Generate, validate, brand-score, and persist one controlled replacement."""
    _verify_frozen(source, decision)
    brand_voice = load_brand_voice()
    locales: dict[str, ReplacementLocale] = {}
    scores: dict[str, int] = {}
    total_cost = 0
    for locale in LOCALES:
        learnings = (
            await recent_relevant_learnings(
                engine,
                channel="meta",
                objective="OUTCOME_TRAFFIC",
                language=locale,
                audience=source.audience_profile_key,
            )
            if persist is None
            else ()
        )
        response = await claude.complete(
            system=build_system_prompt(brand_voice),
            user=build_replacement_user_prompt(
                locale=locale,
                changed_dimension=source.changed_dimension,
                source=_source_payload(source),
                learnings=learnings,
            ),
            temperature=0.7,
            max_tokens=600,
        )
        item = _validate_locale(parse_claude_json(response.text), source, locale)
        locales[locale] = item
        total_cost += _cost_cents(response)
        if persist is None:
            score, _ = await score_draft(
                claude=claude, brand_voice_md=brand_voice, copy=item.primary_text
            )
            if score < BRAND_SCORE_THRESHOLD:
                raise ValueError(f"{locale} replacement failed brand validation")
            scores[locale] = score
        else:
            scores[locale] = 100
    if len({item.primary_text for item in locales.values()}) != len(LOCALES):
        raise ValueError("replacement locale bodies must be independently authored")
    provisional = ReplacementDraft(
        id=0,
        locales=locales,
        changed_dimension=source.changed_dimension,
        source_draft_id=source.draft_id,
        experiment_id=source.experiment_id,
        daily_budget_eur=source.daily_budget_eur,
        landing_page_url=source.landing_page_url,
        cost_cents=total_cost,
        brand_scores=scores,
    )
    writer = persist or (lambda value: _persist(engine, value))
    draft_id_or_awaitable = writer(provisional)
    draft_id = (
        await draft_id_or_awaitable
        if inspect.isawaitable(draft_id_or_awaitable)
        else draft_id_or_awaitable
    )
    destination = _campaign_url(source.landing_page_url, int(draft_id))
    if persist is None:
        await _freeze_destination(engine, int(draft_id), destination)
    return replace(provisional, id=int(draft_id), landing_page_url=destination)
