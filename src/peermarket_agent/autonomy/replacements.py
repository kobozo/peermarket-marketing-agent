"""Schema-first multilingual creative replacements for autonomous Meta experiments."""

from __future__ import annotations

import inspect
import json
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, replace

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from peermarket_agent._json_parse import parse_claude_json
from peermarket_agent.action_contracts import validate_meta
from peermarket_agent.agent.cli_draft import recent_relevant_learnings
from peermarket_agent.autonomy.contracts import DecisionKind, FrozenDecision
from peermarket_agent.autonomy.store import ClaimedAction
from peermarket_agent.brand_quality import BRAND_SCORE_THRESHOLD, score_draft
from peermarket_agent.campaign_urls import build_campaign_url
from peermarket_agent.claude import ClaudeClient
from peermarket_agent.prompts.brand_voice import load_brand_voice
from peermarket_agent.prompts.meta_ad_creative import (
    AUDIENCE_PROFILES,
    _cost_cents,
    build_replacement_system_prompt,
    build_replacement_user_prompt,
)

LOCALES = ("NL", "FR", "EN")
DIMENSIONS = {"hook", "copy", "visual", "audience"}
_LOCALE_MARKER = re.compile(r"(^|\s)(?:\[(?:NL|FR|EN)\]|(?:NL|FR|EN):)(?:\s|$)", re.I)


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
    locale_quality: Mapping[str, dict] | None = None


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


async def _review_native_quality(
    claude: ClaudeClient, locale: str, item: ReplacementLocale
) -> dict:
    response = await claude.complete(
        system=(
            "You are an independent native-language advertising quality reviewer. "
            "Return JSON only; never rewrite the copy."
        ),
        user=(
            f"Requested locale: {locale}. Review the complete creative JSON below, including "
            "hook, body, headline, description, and CTA. Confirm every field is in the exact "
            "requested language (allow the standard Meta CTA platform label), idiomatic, and "
            "not a literal translation. Return exactly: "
            '{"locale":"NL|FR|EN","exact_language":true,"idiomatic":true,'
            '"literal_translation":false,"field_results":{"hook":true,"body":true,'
            '"headline":true,"description":true,"cta_label":true},"evidence":"..."}. '
            f"Creative: {json.dumps(asdict(item), ensure_ascii=False, sort_keys=True)}"
        ),
        temperature=0,
        max_tokens=350,
    )
    payload = parse_claude_json(response.text)
    expected = {
        "locale",
        "exact_language",
        "idiomatic",
        "literal_translation",
        "field_results",
        "evidence",
    }
    fields = {"hook", "body", "headline", "description", "cta_label"}
    if (
        set(payload) != expected
        or payload["locale"] != locale
        or payload["exact_language"] is not True
        or payload["idiomatic"] is not True
        or payload["literal_translation"] is not False
        or not isinstance(payload["field_results"], dict)
        or set(payload["field_results"]) != fields
        or any(payload["field_results"].get(field) is not True for field in fields)
        or not isinstance(payload["evidence"], str)
        or not payload["evidence"].strip()
    ):
        raise ValueError(f"{locale} replacement failed native-quality validation")
    return payload


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
    validate_meta(
        {
            "primary_text": item.primary_text,
            "headline": item.headline,
            "description": item.description,
            "cta_label": item.cta_label,
            "audience_profile_key": item.audience_profile_key,
            "suggested_daily_budget_eur": source.daily_budget_eur,
        },
        allowed_audiences=set(AUDIENCE_PROFILES),
    )
    _validate_native(locale, item)
    return item


async def _persist(
    engine: AsyncEngine, draft: ReplacementDraft, claim: ClaimedAction, generation_token: str
) -> int:
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
        "locale_quality": draft.locale_quality,
    }
    async with engine.begin() as conn:
        action_id = await conn.scalar(
            text("SELECT id FROM action_types WHERE name='meta_ad_creative'")
        )
        if action_id is None:
            raise ValueError("meta_ad_creative action type is not seeded")
        draft_id = await conn.scalar(
            text(
                "INSERT INTO drafts (action_type_id, channel, language, copy, asset_path, generation_cost_cents, brand_score, visual_truthfulness_pass, metadata, autonomous_action_id) "
                "SELECT :action, 'meta', 'MULTI', :copy, :asset, :cost, :score, TRUE, CAST(:metadata AS JSONB), :autonomous_action "
                "WHERE EXISTS (SELECT 1 FROM autonomous_actions a JOIN autonomous_replacement_generations g ON g.action_id=a.id WHERE a.id=:autonomous_action AND a.status IN ('leased','executing') AND a.lease_owner=:owner AND a.lease_token=:claim_token AND a.lease_expires_at>NOW() AND g.state='generating' AND g.lease_owner=:owner AND g.lease_token=:generation_token AND g.lease_expires_at>NOW()) RETURNING id"
            ),
            {
                "action": action_id,
                "copy": "\n\n".join(v.complete_text() for v in draft.locales.values()),
                "asset": draft.asset_path,
                "cost": draft.cost_cents,
                "score": min(draft.brand_scores.values()),
                "metadata": json.dumps(metadata),
                "autonomous_action": claim.id,
                "owner": claim.lease_owner,
                "claim_token": claim.lease_token,
                "generation_token": generation_token,
            },
        )
        if draft_id is None:
            raise RuntimeError("replacement generation ownership was lost")
        destination = build_campaign_url(draft.landing_page_url, int(draft_id))
        await conn.execute(
            text(
                "UPDATE drafts SET metadata=metadata || jsonb_build_object('landing_page_url', :url) "
                "WHERE id=:id AND autonomous_action_id=:action"
            ),
            {"url": destination, "id": draft_id, "action": claim.id},
        )
        completed = await conn.execute(
            text(
                "UPDATE autonomous_replacement_generations g SET state='completed', replacement_draft_id=:draft, lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL, updated_at=NOW() WHERE action_id=:action AND state='generating' AND lease_owner=:owner AND lease_token=:token AND lease_expires_at>NOW() AND EXISTS (SELECT 1 FROM autonomous_actions a WHERE a.id=g.action_id AND a.status IN ('leased','executing') AND a.lease_owner=:owner AND a.lease_token=:claim_token AND a.lease_expires_at>NOW())"
            ),
            {
                "draft": draft_id,
                "action": claim.id,
                "owner": claim.lease_owner,
                "token": generation_token,
                "claim_token": claim.lease_token,
            },
        )
        if completed.rowcount != 1:
            raise RuntimeError("replacement generation ownership was lost")
    return int(draft_id)


async def _claim_generation(engine: AsyncEngine, claim: ClaimedAction) -> tuple[str, int | None]:
    token = uuid.uuid4().hex
    async with engine.begin() as conn:
        if (
            await conn.execute(
                text(
                    "SELECT id FROM autonomous_actions WHERE id=:id AND status IN ('leased','executing') AND lease_owner=:owner AND lease_token=:token AND lease_expires_at>NOW() FOR UPDATE"
                ),
                {"id": claim.id, "owner": claim.lease_owner, "token": claim.lease_token},
            )
        ).first() is None:
            raise RuntimeError("replacement action ownership was lost")
        inserted = (
            (
                await conn.execute(
                    text(
                        "INSERT INTO autonomous_replacement_generations (action_id,lease_owner,lease_token,lease_expires_at) VALUES (:action,:owner,:token,NOW()+INTERVAL '5 minutes') ON CONFLICT (action_id) DO NOTHING RETURNING state,replacement_draft_id"
                    ),
                    {"action": claim.id, "owner": claim.lease_owner, "token": token},
                )
            )
            .mappings()
            .first()
        )
        if inserted is not None:
            return token, None
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT state,replacement_draft_id FROM autonomous_replacement_generations WHERE action_id=:action FOR UPDATE"
                    ),
                    {"action": claim.id},
                )
            )
            .mappings()
            .one()
        )
        if row["state"] == "completed":
            return token, int(row["replacement_draft_id"])
        reclaimed = await conn.execute(
            text(
                "UPDATE autonomous_replacement_generations SET lease_owner=:owner,lease_token=:token,lease_expires_at=NOW()+INTERVAL '5 minutes',updated_at=NOW() WHERE action_id=:action AND lease_expires_at<=NOW()"
            ),
            {"action": claim.id, "owner": claim.lease_owner, "token": token},
        )
        if reclaimed.rowcount != 1:
            raise RuntimeError("replacement generation is owned by another worker")
        return token, None


async def _load_replacement(engine: AsyncEngine, draft_id: int) -> ReplacementDraft:
    async with engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT metadata,generation_cost_cents,brand_score FROM drafts WHERE id=:id"
                    ),
                    {"id": draft_id},
                )
            )
            .mappings()
            .one()
        )
    m = row["metadata"]
    locales = {key: ReplacementLocale(**value) for key, value in m["locales"].items()}
    return ReplacementDraft(
        draft_id,
        locales,
        m["changed_dimension"],
        m["source_draft_id"],
        m["source_campaign_id"],
        m["experiment_id"],
        m["audience_profile_key"],
        m["image_prompt"],
        m["asset_path"],
        m["suggested_daily_budget_eur"],
        m["landing_page_url"],
        row["generation_cost_cents"],
        {key: row["brand_score"] for key in locales},
        m.get("locale_quality"),
    )


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
    claim: ClaimedAction | None,
    persist: Callable[[ReplacementDraft], int | Awaitable[int]] | None = None,
) -> ReplacementDraft:
    """Generate, validate, score the complete creatives, then persist one bundle draft."""
    _verify_frozen(source, decision)
    generation_token: str | None = None
    if persist is None:
        if not isinstance(claim, ClaimedAction):
            raise TypeError("production replacement generation requires a ClaimedAction")
        generation_token, completed_id = await _claim_generation(engine, claim)
        if completed_id is not None:
            return await _load_replacement(engine, completed_id)
    brand_voice = load_brand_voice()
    locales: dict[str, ReplacementLocale] = {}
    scores: dict[str, int] = {}
    quality: dict[str, dict] = {}
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
            system=build_replacement_system_prompt(brand_voice),
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
        quality[locale] = await _review_native_quality(claude, locale, item)
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
        locale_quality=quality,
    )
    writer = persist or (lambda value: _persist(engine, value, claim, generation_token))
    result = writer(provisional)
    draft_id = await result if inspect.isawaitable(result) else result
    destination = build_campaign_url(source.landing_page_url, int(draft_id))
    return replace(provisional, id=int(draft_id), landing_page_url=destination)
