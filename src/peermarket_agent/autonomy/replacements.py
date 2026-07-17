"""Schema-first multilingual creative replacements for autonomous Meta experiments."""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, replace

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from peermarket_agent._json_parse import parse_claude_json
from peermarket_agent.agent.cli_draft import recent_relevant_learnings
from peermarket_agent.autonomy.contracts import DecisionKind, FrozenDecision
from peermarket_agent.brand_quality import BRAND_SCORE_THRESHOLD, score_draft
from peermarket_agent.campaign_urls import build_campaign_url
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
_LOCALE_MARKER = re.compile(r"(^|\s)(?:\[(?:NL|FR|EN)\]|(?:NL|FR|EN):)(?:\s|$)", re.I)
_LANGUAGE_SIGNALS = {
    "NL": re.compile(r"\b(?:de|het|een|voor|van|met|je|jouw|veilig|buurt)\b", re.I),
    "FR": re.compile(r"\b(?:le|la|les|des|une|pour|avec|vous|votre|près)\b", re.I),
    "EN": re.compile(r"\b(?:the|a|an|for|with|you|your|safe|nearby|people)\b", re.I),
}


@dataclass(frozen=True, slots=True)
class LocaleCreative:
    locale: str
    hook: str
    body: str
    headline: str
    description: str
    cta_label: str

    @property
    def primary_text(self) -> str:
        return f"{self.hook}\n\n{self.body}"

    def complete_text(self) -> str:
        return "\n".join((self.hook, self.body, self.headline, self.description, self.cta_label))


@dataclass(frozen=True, slots=True)
class ReplacementSource:
    draft_id: int
    campaign_id: str
    experiment_id: str
    changed_dimension: str
    locales: Mapping[str, LocaleCreative]
    audience_profile_key: str
    image_prompt: str
    asset_path: str
    daily_budget_eur: int
    landing_page_url: str

    def frozen_evidence(self) -> dict:
        return {"source": asdict(self)}

    def validate_baseline(self) -> None:
        if self.changed_dimension not in DIMENSIONS:
            raise ValueError("invalid frozen replacement dimension")
        if set(self.locales) != set(LOCALES):
            raise ValueError("source requires exact NL/FR/EN multilingual baseline")
        if any(item.locale != locale for locale, item in self.locales.items()):
            raise ValueError("source locale keys and payloads differ")
        if self.changed_dimension == "visual" and (
            not self.image_prompt.strip() or not self.asset_path.strip()
        ):
            raise ValueError("visual baseline is required for visual experiments")
        if self.changed_dimension in {"hook", "copy", "visual", "audience"} and any(
            not field.strip()
            for item in self.locales.values()
            for field in (
                item.hook,
                item.body,
                item.headline,
                item.description,
                item.cta_label,
            )
        ):
            raise ValueError("complete multilingual text baseline is required")
        if self.audience_profile_key not in AUDIENCE_PROFILES:
            raise ValueError("invalid source audience")


@dataclass(frozen=True, slots=True)
class ReplacementLocale(LocaleCreative):
    audience_profile_key: str
    image_prompt: str
    asset_path: str


@dataclass(frozen=True, slots=True)
class ReplacementDraft:
    id: int
    locales: Mapping[str, ReplacementLocale]
    changed_dimension: str
    source_draft_id: int
    source_campaign_id: str
    experiment_id: str
    audience_profile_key: str
    image_prompt: str
    asset_path: str
    daily_budget_eur: int
    landing_page_url: str
    cost_cents: int
    brand_scores: Mapping[str, int]


def _verify_frozen(source: ReplacementSource, decision: FrozenDecision) -> None:
    source.validate_baseline()
    build_campaign_url(source.landing_page_url, 1)
    if decision.kind is not DecisionKind.REPLACE or decision.campaign_id != source.campaign_id:
        raise ValueError("replacement does not match frozen REPLACE decision")
    if decision.evidence.get("source") != asdict(source):
        raise ValueError("source metadata differs from frozen decision evidence")
    if type(source.daily_budget_eur) is not int or not 5 <= source.daily_budget_eur <= 20:
        raise ValueError("frozen daily budget must be an integer from 5-20")


def _normalized(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _validate_native(locale: str, item: ReplacementLocale) -> None:
    complete = item.complete_text()
    if _LOCALE_MARKER.search(complete):
        raise ValueError("replacement contains a literal locale marker")
    # Conservative signal, not a claim of semantic language detection.
    if not _LANGUAGE_SIGNALS[locale].search(item.body):
        raise ValueError(f"{locale} replacement lacks native-language validation signals")


def _validate_locale(payload: dict, source: ReplacementSource, locale: str) -> ReplacementLocale:
    expected = {
        "locale",
        "changed_dimension",
        "hook",
        "body",
        "headline",
        "description",
        "cta_label",
        "audience_profile_key",
        "image_prompt",
        "asset_path",
        "suggested_daily_budget_eur",
    }
    if set(payload) != expected:
        raise ValueError("replacement JSON schema has missing or extra fields")
    if payload["locale"] != locale or payload["changed_dimension"] != source.changed_dimension:
        raise ValueError("replacement locale or dimension differs from request")
    if payload["suggested_daily_budget_eur"] != source.daily_budget_eur:
        raise ValueError("replacement changed frozen budget")
    for field in (
        "hook",
        "body",
        "headline",
        "description",
        "cta_label",
        "image_prompt",
        "asset_path",
    ):
        if not isinstance(payload[field], str) or not payload[field].strip():
            raise ValueError(f"replacement {field} must be non-empty")
    if payload["cta_label"] not in {"Learn More", "Sign Up", "Shop Now", "Get Started"}:
        raise ValueError("replacement cta_label is invalid")
    if payload["audience_profile_key"] not in AUDIENCE_PROFILES:
        raise ValueError("replacement audience is invalid")
    original = source.locales[locale]
    fields = {
        "hook": (payload["hook"], original.hook),
        "body": (payload["body"], original.body),
        "headline": (payload["headline"], original.headline),
        "description": (payload["description"], original.description),
        "cta_label": (payload["cta_label"], original.cta_label),
        "audience_profile_key": (payload["audience_profile_key"], source.audience_profile_key),
        "image_prompt": (payload["image_prompt"], source.image_prompt),
        "asset_path": (payload["asset_path"], source.asset_path),
    }
    mutable = {
        "hook": {"hook"},
        "copy": {"body", "headline", "description", "cta_label"},
        "visual": {"image_prompt", "asset_path"},
        "audience": {"audience_profile_key"},
    }[source.changed_dimension]
    changed = {name for name, (new, old) in fields.items() if new != old}
    if not changed or not changed <= mutable:
        raise ValueError("replacement must change exactly one primary dimension")
    if source.changed_dimension == "copy" and changed != mutable:
        raise ValueError("copy replacement must change body, headline, description, and CTA")
    item = ReplacementLocale(
        locale=locale,
        hook=payload["hook"],
        body=payload["body"],
        headline=payload["headline"],
        description=payload["description"],
        cta_label=payload["cta_label"],
        audience_profile_key=payload["audience_profile_key"],
        image_prompt=payload["image_prompt"],
        asset_path=payload["asset_path"],
    )
    _validate_native(locale, item)
    return item


async def _persist(engine: AsyncEngine, draft: ReplacementDraft) -> int:
    metadata = {
        "autonomous_replacement": True,
        "source_draft_id": draft.source_draft_id,
        "source_campaign_id": draft.source_campaign_id,
        "experiment_id": draft.experiment_id,
        "changed_dimension": draft.changed_dimension,
        "locales": {key: asdict(value) for key, value in draft.locales.items()},
        "audience_profile_key": draft.audience_profile_key,
        "image_prompt": draft.image_prompt,
        "asset_path": draft.asset_path,
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
                "INSERT INTO drafts (action_type_id, channel, language, copy, asset_path, generation_cost_cents, brand_score, visual_truthfulness_pass, metadata) VALUES (:action, 'meta', 'MULTI', :copy, :asset, :cost, :score, TRUE, CAST(:metadata AS JSONB)) RETURNING id"
            ),
            {
                "action": action_id,
                "copy": "\n\n".join(v.complete_text() for v in draft.locales.values()),
                "asset": draft.asset_path,
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
                "UPDATE drafts SET metadata=metadata || jsonb_build_object('landing_page_url', :url) WHERE id=:id AND metadata->>'autonomous_replacement'='true'"
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
    """Generate, validate, score the complete creatives, then persist one bundle draft."""
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
                creative_dimension=source.changed_dimension,
            )
            if persist is None
            else ()
        )
        response = await claude.complete(
            system=build_system_prompt(brand_voice),
            user=build_replacement_user_prompt(
                locale=locale,
                changed_dimension=source.changed_dimension,
                source={
                    "locale": asdict(source.locales[locale]),
                    "audience_profile_key": source.audience_profile_key,
                    "image_prompt": source.image_prompt,
                    "asset_path": source.asset_path,
                    "suggested_daily_budget_eur": source.daily_budget_eur,
                },
                learnings=learnings,
            ),
            temperature=0.7,
            max_tokens=700,
        )
        item = _validate_locale(parse_claude_json(response.text), source, locale)
        locales[locale] = item
        total_cost += _cost_cents(response)
        score, _ = await score_draft(
            claude=claude, brand_voice_md=brand_voice, copy=item.complete_text()
        )
        if score < BRAND_SCORE_THRESHOLD:
            raise ValueError(f"{locale} complete replacement failed brand validation")
        scores[locale] = int(score)
    normalized = [_normalized(item.complete_text()) for item in locales.values()]
    if len(set(normalized)) != len(LOCALES):
        raise ValueError("literal translation detector rejected cross-locale sameness")
    if len({item.audience_profile_key for item in locales.values()}) != 1:
        raise ValueError("replacement bundle requires one coherent audience")
    if len({(item.image_prompt, item.asset_path) for item in locales.values()}) != 1:
        raise ValueError("replacement bundle requires one coherent visual")
    provisional = ReplacementDraft(
        id=0,
        locales=locales,
        changed_dimension=source.changed_dimension,
        source_draft_id=source.draft_id,
        source_campaign_id=source.campaign_id,
        experiment_id=source.experiment_id,
        audience_profile_key=next(iter(locales.values())).audience_profile_key,
        image_prompt=next(iter(locales.values())).image_prompt,
        asset_path=next(iter(locales.values())).asset_path,
        daily_budget_eur=source.daily_budget_eur,
        landing_page_url=source.landing_page_url,
        cost_cents=total_cost,
        brand_scores=scores,
    )
    writer = persist or (lambda value: _persist(engine, value))
    result = writer(provisional)
    draft_id = await result if inspect.isawaitable(result) else result
    destination = build_campaign_url(source.landing_page_url, int(draft_id))
    if persist is None:
        await _freeze_destination(engine, int(draft_id), destination)
    return replace(provisional, id=int(draft_id), landing_page_url=destination)
