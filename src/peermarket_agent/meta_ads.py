"""Meta Marketing API connector for creating and activating approved ads."""

import asyncio
import base64
import hashlib
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from urllib.parse import urlencode

import structlog
from facebook_business.adobjects.ad import Ad
from facebook_business.adobjects.adaccount import AdAccount
from facebook_business.adobjects.adcreative import AdCreative
from facebook_business.adobjects.adset import AdSet
from facebook_business.adobjects.campaign import Campaign
from facebook_business.api import FacebookAdsApi
from facebook_business.exceptions import FacebookRequestError

log = structlog.get_logger(__name__)


_ALLOWED_CTA_TYPES = {"LEARN_MORE", "SIGN_UP", "SHOP_NOW", "GET_STARTED"}


@dataclass(frozen=True)
class MetaConfig:
    app_id: str
    app_secret: str
    system_user_token: str
    ad_account_id: str  # 'act_<numeric>'
    page_id: str


@dataclass(frozen=True)
class MetaAdResult:
    ad_id: str
    ad_set_id: str
    campaign_id: str
    creative_id: str
    ads_manager_url: str
    status: str


@dataclass(frozen=True)
class MetaBundleLocale:
    primary_text: str
    headline: str
    description: str
    cta_type: str
    image_bytes: bytes | None


@dataclass(frozen=True)
class MetaReplacementBundleResult:
    campaign_id: str
    ad_set_id: str
    creative_ids: Mapping[str, str]
    ad_ids: Mapping[str, str]
    ads_manager_url: str
    status: str = "PAUSED"
    image_hashes: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class MetaActivationResult:
    campaign: dict[str, str]
    ad_set: dict[str, str]
    ad: dict[str, str]


class MetaAdsDisabled(RuntimeError):
    """Meta connector cannot operate — credentials missing."""


class MetaAdsError(RuntimeError):
    """Any Meta API failure, with sanitized reconciliation context."""

    def __init__(
        self,
        message: str,
        *,
        phase: str | None = None,
        resource_ids: dict[str, str] | None = None,
        observed_statuses: dict[str, dict[str, str | int]] | None = None,
        rollback_errors: dict[str, str] | None = None,
        api_error_code: int | None = None,
        api_error_subcode: int | None = None,
        http_status: int | None = None,
        api_error_type: str | None = None,
    ) -> None:
        self.phase = phase
        self.resource_ids = resource_ids or {}
        self.observed_statuses = observed_statuses or {}
        self.rollback_errors = rollback_errors or {}
        self.api_error_code = api_error_code
        self.api_error_subcode = api_error_subcode
        self.http_status = http_status
        self.api_error_type = api_error_type
        details = {
            "phase": phase,
            "resource_ids": self.resource_ids,
            "observed_statuses": self.observed_statuses,
            "rollback_errors": self.rollback_errors,
        }
        super().__init__(f"{message}: {details}")


# Targeting templates per audience profile. Belgium-only, NL+FR.
# These mirror the AUDIENCE_PROFILES in prompts/meta_ad_creative.py but
# in Meta's actual targeting-spec JSON shape.
_TARGETING_TEMPLATES = {
    "declutterers": {
        "age_min": 28,
        "age_max": 55,
        "geo_locations": {"countries": ["BE"]},
        "targeting_automation": {"advantage_audience": 0},
        "locales": [5, 24],  # Dutch (5), French (24) — Meta locale IDs for Belgium
        "publisher_platforms": ["facebook", "instagram"],
        "facebook_positions": ["feed", "story"],
        "instagram_positions": ["stream", "story", "reels"],
    },
    "trust_conscious_locals": {
        "age_min": 35,
        "age_max": 65,
        "geo_locations": {"countries": ["BE"]},
        "targeting_automation": {"advantage_audience": 0},
        "locales": [5, 24],
        "publisher_platforms": ["facebook", "instagram"],
        "facebook_positions": ["feed"],
        "instagram_positions": ["stream"],
    },
}


def _ensure_enabled(config: MetaConfig) -> None:
    missing = [
        k
        for k, v in {
            "app_id": config.app_id,
            "app_secret": config.app_secret,
            "system_user_token": config.system_user_token,
            "ad_account_id": config.ad_account_id,
            "page_id": config.page_id,
        }.items()
        if not v
    ]
    if missing:
        raise MetaAdsDisabled(
            f"Meta connector disabled — missing credentials: {missing}. "
            "Set META_APP_ID, META_APP_SECRET, META_SYSTEM_USER_TOKEN, "
            "META_AD_ACCOUNT_ID, META_PAGE_ID."
        )


def _init_api(config: MetaConfig) -> FacebookAdsApi:
    """Initialize the global FacebookAdsApi singleton. Safe to call repeatedly."""
    return FacebookAdsApi.init(
        app_id=config.app_id,
        app_secret=config.app_secret,
        access_token=config.system_user_token,
    )


def _build_ads_manager_url(ad_account_id: str, ad_id: str) -> str:
    # ad_account_id is 'act_123'; URL wants the bare numeric in act= and the id in selected_ad_ids=
    numeric_act = ad_account_id.removeprefix("act_")
    q = urlencode({"act": numeric_act, "selected_ad_ids": ad_id})
    return f"https://business.facebook.com/adsmanager/manage/ads?{q}"


def _sync_create(
    *,
    config: MetaConfig,
    name: str,
    primary_text: str,
    headline: str,
    description: str,
    cta_type: str,
    landing_page_url: str,
    image_bytes: bytes | None,
    audience_profile_key: str,
    daily_budget_eur: int,
) -> MetaAdResult:
    _ensure_enabled(config)
    if cta_type not in _ALLOWED_CTA_TYPES:
        raise MetaAdsError(
            f"cta_type {cta_type!r} not allowed (must be one of {_ALLOWED_CTA_TYPES})"
        )
    if audience_profile_key not in _TARGETING_TEMPLATES:
        raise MetaAdsError(
            f"unknown audience profile {audience_profile_key!r} "
            f"(valid: {list(_TARGETING_TEMPLATES.keys())})"
        )
    _init_api(config)
    account = AdAccount(config.ad_account_id)
    phase = "create_campaign"
    resource_ids: dict[str, str] = {}

    try:
        # 1) Campaign — paused, traffic objective, no special ad categories
        campaign = account.create_campaign(
            params={
                Campaign.Field.name: f"{name} — campaign",
                Campaign.Field.objective: "OUTCOME_TRAFFIC",
                Campaign.Field.status: Campaign.Status.paused,
                Campaign.Field.special_ad_categories: [],
                "is_adset_budget_sharing_enabled": False,
            },
            fields=[Campaign.Field.id],
        )
        campaign_id = campaign["id"]
        resource_ids["campaign_id"] = campaign_id
        log.info("meta_ads.campaign_created", campaign_id=campaign_id)

        # 2) AdSet — targeting + budget in cents
        phase = "create_ad_set"
        adset = account.create_ad_set(
            params={
                AdSet.Field.name: f"{name} — adset",
                AdSet.Field.campaign_id: campaign_id,
                AdSet.Field.daily_budget: daily_budget_eur * 100,
                AdSet.Field.billing_event: "IMPRESSIONS",
                AdSet.Field.optimization_goal: "LINK_CLICKS",
                AdSet.Field.bid_strategy: "LOWEST_COST_WITHOUT_CAP",
                AdSet.Field.targeting: _TARGETING_TEMPLATES[audience_profile_key],
                AdSet.Field.status: AdSet.Status.paused,
            },
            fields=[AdSet.Field.id],
        )
        adset_id = adset["id"]
        resource_ids["ad_set_id"] = adset_id
        log.info("meta_ads.adset_created", adset_id=adset_id)

        # 3) Image upload (optional)
        image_hash: str | None = None
        if image_bytes:
            phase = "upload_image"
            b64 = base64.b64encode(image_bytes).decode("ascii")
            image = account.create_ad_image(
                params={"bytes": b64},
                fields=["hash"],
            )
            image_hash = image["hash"]
            log.info("meta_ads.image_uploaded", image_hash=image_hash)

        # 4) Creative
        phase = "create_creative"
        link_data: dict = {
            "message": primary_text,
            "link": landing_page_url,
            "name": headline,
            "description": description,
            "call_to_action": {"type": cta_type},
        }
        if image_hash:
            link_data["image_hash"] = image_hash
        creative = account.create_ad_creative(
            params={
                AdCreative.Field.name: f"{name} — creative",
                AdCreative.Field.object_story_spec: {
                    "page_id": config.page_id,
                    "link_data": link_data,
                },
            },
            fields=[AdCreative.Field.id],
        )
        creative_id = creative["id"]
        resource_ids["creative_id"] = creative_id
        log.info("meta_ads.creative_created", creative_id=creative_id)

        # 5) Ad
        phase = "create_ad"
        ad = account.create_ad(
            params={
                Ad.Field.name: name,
                Ad.Field.adset_id: adset_id,
                Ad.Field.creative: {"creative_id": creative_id},
                Ad.Field.status: Ad.Status.paused,
            },
            fields=[Ad.Field.id],
        )
        ad_id = ad["id"]
        resource_ids["ad_id"] = ad_id
        log.info("meta_ads.ad_created", ad_id=ad_id)

        return MetaAdResult(
            ad_id=ad_id,
            ad_set_id=adset_id,
            campaign_id=campaign_id,
            creative_id=creative_id,
            ads_manager_url=_build_ads_manager_url(config.ad_account_id, ad_id),
            status="PAUSED",
        )
    except FacebookRequestError as e:
        details = [e.api_error_message() or e.get_message()]
        if e.api_error_code() is not None:
            details.append(f"code={e.api_error_code()}")
        if e.api_error_subcode() is not None:
            details.append(f"subcode={e.api_error_subcode()}")
        body = e.body()
        api_error = body.get("error", {}) if isinstance(body, dict) else {}
        if user_title := api_error.get("error_user_title"):
            details.append(f"user_title={user_title}")
        if user_message := api_error.get("error_user_msg"):
            details.append(f"user_message={user_message}")
        rollback_errors = _sync_pause(config, resource_ids)
        raise MetaAdsError(
            f"Meta API error: {_redact_credentials('; '.join(details), config)}",
            phase=phase,
            resource_ids=resource_ids,
            rollback_errors=rollback_errors,
        ) from None


async def create_meta_ad_paused(
    *,
    config: MetaConfig,
    name: str,
    primary_text: str,
    headline: str,
    description: str,
    cta_type: str,
    landing_page_url: str,
    image_bytes: bytes | None,
    audience_profile_key: str,
    daily_budget_eur: int,
) -> MetaAdResult:
    """Create a Meta campaign + adset + creative + ad, all in PAUSED state.

    Raises:
        MetaAdsDisabled: if config has empty credentials.
        MetaAdsError: any Meta API failure or invalid input.
    """
    return await asyncio.to_thread(
        _sync_create,
        config=config,
        name=name,
        primary_text=primary_text,
        headline=headline,
        description=description,
        cta_type=cta_type,
        landing_page_url=landing_page_url,
        image_bytes=image_bytes,
        audience_profile_key=audience_profile_key,
        daily_budget_eur=daily_budget_eur,
    )


def _sync_create_bundle_resource(
    *,
    config: MetaConfig,
    name: str,
    audience_profile_key: str,
    daily_budget_eur: int,
    landing_page_url: str,
    locale: str | None,
    creative: MetaBundleLocale | None,
    progress: dict,
) -> tuple[str, str]:
    """Create exactly one missing bundle resource and return its durable progress key/value."""
    _ensure_enabled(config)
    if audience_profile_key not in _TARGETING_TEMPLATES:
        raise MetaAdsError("unknown audience profile", phase="validate_bundle")
    api = _init_api(config)
    account = AdAccount(config.ad_account_id, api=api)

    def existing(
        getter_name: str,
        resource_name: str,
        *,
        fields: list[str],
        expected: Mapping[str, object],
        id_field: str = "id",
    ) -> str | None:
        getter = getattr(account, getter_name)
        named = [
            candidate
            for candidate in getter(fields=fields)
            if candidate.get("name") == resource_name
        ]
        if not named:
            return None
        if len(named) != 1:
            raise MetaAdsError(
                "ambiguous Meta bundle identity during reconciliation",
                phase="reconcile_bundle_identity",
            )
        candidate = named[0]
        if any(candidate.get(key) != value for key, value in expected.items()):
            raise MetaAdsError(
                "Meta bundle identity mismatch during reconciliation",
                phase="reconcile_bundle_identity",
            )
        value = candidate.get(id_field)
        if not isinstance(value, str) or not value:
            raise MetaAdsError(
                "Meta bundle identity is incomplete during reconciliation",
                phase="reconcile_bundle_identity",
            )
        return value

    if "campaign_id" not in progress:
        resource_name = f"{name} — campaign"
        account_id = config.ad_account_id.removeprefix("act_")
        if found := existing(
            "get_campaigns",
            resource_name,
            fields=["id", "name", "objective", "special_ad_categories", "account_id"],
            expected={
                "objective": "OUTCOME_TRAFFIC",
                "special_ad_categories": [],
                "account_id": account_id,
            },
        ):
            return "campaign_id", found
        item = account.create_campaign(
            params={
                Campaign.Field.name: resource_name,
                Campaign.Field.objective: "OUTCOME_TRAFFIC",
                Campaign.Field.status: Campaign.Status.paused,
                Campaign.Field.special_ad_categories: [],
                "is_adset_budget_sharing_enabled": False,
            },
            fields=[Campaign.Field.id],
        )
        return "campaign_id", item["id"]
    if "ad_set_id" not in progress:
        resource_name = f"{name} — adset"
        expected_targeting = _TARGETING_TEMPLATES[audience_profile_key]
        if found := existing(
            "get_ad_sets",
            resource_name,
            fields=[
                "id",
                "name",
                "campaign_id",
                "daily_budget",
                "billing_event",
                "optimization_goal",
                "bid_strategy",
                "targeting",
                "destination_type",
            ],
            expected={
                "campaign_id": progress["campaign_id"],
                "daily_budget": str(daily_budget_eur * 100),
                "billing_event": "IMPRESSIONS",
                "optimization_goal": "LINK_CLICKS",
                "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
                "targeting": expected_targeting,
                "destination_type": "WEBSITE",
            },
        ):
            return "ad_set_id", found
        item = account.create_ad_set(
            params={
                AdSet.Field.name: resource_name,
                AdSet.Field.campaign_id: progress["campaign_id"],
                AdSet.Field.daily_budget: daily_budget_eur * 100,
                AdSet.Field.billing_event: "IMPRESSIONS",
                AdSet.Field.optimization_goal: "LINK_CLICKS",
                AdSet.Field.bid_strategy: "LOWEST_COST_WITHOUT_CAP",
                AdSet.Field.targeting: _TARGETING_TEMPLATES[audience_profile_key],
                AdSet.Field.destination_type: "WEBSITE",
                AdSet.Field.status: AdSet.Status.paused,
            },
            fields=[AdSet.Field.id],
        )
        return "ad_set_id", item["id"]
    if locale is None or creative is None:
        raise MetaAdsError("locale required for bundle child", phase="validate_bundle")
    image_key = f"image_hash:{locale}"
    creative_key = f"creative_id:{locale}"
    ad_key = f"ad_id:{locale}"
    if creative.image_bytes and image_key not in progress:
        image_name = (
            f"{name} {locale} image {hashlib.sha256(creative.image_bytes).hexdigest()[:20]}"
        )
        if found := existing(
            "get_ad_images", image_name, fields=["hash", "name"], expected={}, id_field="hash"
        ):
            return image_key, found
        image = account.create_ad_image(
            params={
                "bytes": base64.b64encode(creative.image_bytes).decode("ascii"),
                "name": image_name,
            },
            fields=["hash"],
        )
        return image_key, image["hash"]
    if creative_key not in progress:
        if creative.cta_type not in _ALLOWED_CTA_TYPES:
            raise MetaAdsError("invalid bundle CTA", phase="validate_bundle")
        link_data = {
            "message": creative.primary_text,
            "link": landing_page_url,
            "name": creative.headline,
            "description": creative.description,
            "call_to_action": {"type": creative.cta_type},
        }
        if image_key in progress:
            link_data["image_hash"] = progress[image_key]
        resource_name = f"{name} {locale} — creative"
        story_spec = {"page_id": config.page_id, "link_data": link_data}
        if found := existing(
            "get_ad_creatives",
            resource_name,
            fields=["id", "name", "object_story_spec"],
            expected={"object_story_spec": story_spec},
        ):
            return creative_key, found
        item = account.create_ad_creative(
            params={
                AdCreative.Field.name: resource_name,
                AdCreative.Field.object_story_spec: {
                    "page_id": config.page_id,
                    "link_data": link_data,
                },
            },
            fields=[AdCreative.Field.id],
        )
        return creative_key, item["id"]
    resource_name = f"{name} {locale}"
    if found := existing(
        "get_ads",
        resource_name,
        fields=["id", "name", "adset_id", "creative"],
        expected={"adset_id": progress["ad_set_id"], "creative": {"id": progress[creative_key]}},
    ):
        return ad_key, found
    item = account.create_ad(
        params={
            Ad.Field.name: resource_name,
            Ad.Field.adset_id: progress["ad_set_id"],
            Ad.Field.creative: {"creative_id": progress[creative_key]},
            Ad.Field.status: Ad.Status.paused,
        },
        fields=[Ad.Field.id],
    )
    return ad_key, item["id"]


async def create_meta_replacement_bundle_paused(
    *,
    config: MetaConfig,
    name: str,
    locales: Mapping[str, MetaBundleLocale],
    landing_page_url: str,
    audience_profile_key: str,
    daily_budget_eur: int,
    progress: Mapping[str, str] | None = None,
    persist_progress: Callable[[str, str], Awaitable[None]],
) -> MetaReplacementBundleResult:
    """Idempotently create one paused campaign/adset and exactly NL/FR/EN ads."""
    if set(locales) != {"NL", "FR", "EN"}:
        raise ValueError("Meta replacement bundle requires exact NL/FR/EN locales")
    current = dict(progress or {})
    for locale in (None, None, "NL", "FR", "EN"):
        child = locales.get(locale) if locale else None
        while (locale is None and ("campaign_id" not in current or "ad_set_id" not in current)) or (
            locale is not None and f"ad_id:{locale}" not in current
        ):
            try:
                if "campaign_id" not in current:
                    request_key, request_name = "request_name:campaign", f"{name} — campaign"
                elif "ad_set_id" not in current:
                    request_key, request_name = "request_name:ad_set", f"{name} — adset"
                elif locale is not None and f"creative_id:{locale}" not in current:
                    request_key, request_name = (
                        f"request_name:creative:{locale}",
                        f"{name} {locale} — creative",
                    )
                else:
                    request_key, request_name = f"request_name:ad:{locale}", f"{name} {locale}"
                if request_key not in current:
                    current[request_key] = request_name
                    await persist_progress(request_key, request_name)
                key, value = await asyncio.to_thread(
                    _sync_create_bundle_resource,
                    config=config,
                    name=name,
                    audience_profile_key=audience_profile_key,
                    daily_budget_eur=daily_budget_eur,
                    landing_page_url=landing_page_url,
                    locale=locale,
                    creative=child,
                    progress=current,
                )
                current[key] = value
                await persist_progress(key, value)
            except MetaAdsError:
                raise
            except Exception:
                raise MetaAdsError(
                    "Meta bundle creation failed",
                    phase="create_bundle",
                    resource_ids={
                        key: value for key, value in current.items() if "hash" not in key
                    },
                ) from None
    ad_ids = {locale: current[f"ad_id:{locale}"] for locale in ("NL", "FR", "EN")}
    return MetaReplacementBundleResult(
        campaign_id=current["campaign_id"],
        ad_set_id=current["ad_set_id"],
        creative_ids={locale: current[f"creative_id:{locale}"] for locale in ("NL", "FR", "EN")},
        ad_ids=ad_ids,
        ads_manager_url=_build_ads_manager_url(config.ad_account_id, ad_ids["NL"]),
        image_hashes={
            locale: current[f"image_hash:{locale}"]
            for locale in ("NL", "FR", "EN")
            if f"image_hash:{locale}" in current
        },
    )


def _sync_get_replacement_bundle_statuses(
    config: MetaConfig,
    campaign_id: str,
    ad_set_id: str,
    ad_ids: Mapping[str, str],
    *,
    creative_ids: Mapping[str, str] | None = None,
    landing_page_url: str | None = None,
    locales: Mapping[str, MetaBundleLocale] | None = None,
    image_hashes: Mapping[str, str] | None = None,
) -> dict[str, dict[str, str | int]]:
    _ensure_enabled(config)
    _init_api(config)
    locale_keys = set(ad_ids)
    if locale_keys != {"NL", "FR", "EN"} and creative_ids is None:
        raise MetaAdsError("exact NL/FR/EN ad IDs required", phase="verify_bundle")
    if creative_ids is not None and (
        set(creative_ids) != locale_keys
        or locales is None
        or set(locales) != locale_keys
        or landing_page_url is None
    ):
        raise MetaAdsError("complete frozen creative identity required", phase="verify_bundle")
    result: dict[str, dict[str, str | int]] = {
        "campaign": dict(Campaign(campaign_id).api_get(fields=["status", "effective_status"])),
        "ad_set": dict(
            AdSet(ad_set_id).api_get(
                fields=["status", "effective_status", "daily_budget", "campaign_id"]
            )
        ),
    }
    result["ad_set"] = {**result["ad_set"], **_normalized_daily_budget(result["ad_set"])}
    if creative_ids is not None and str(result["ad_set"].get("campaign_id")) != campaign_id:
        raise MetaAdsError("Meta bundle parent identity mismatch", phase="verify_bundle")
    for locale, ad_id in ad_ids.items():
        ad = dict(Ad(ad_id).api_get(fields=["status", "effective_status", "adset_id", "creative"]))
        result[f"ad:{locale}"] = ad
        if creative_ids is not None and str(ad.get("adset_id")) != ad_set_id:
            raise MetaAdsError("Meta bundle ad parent identity mismatch", phase="verify_bundle")
        if creative_ids is not None:
            creative_id = creative_ids[locale]
            if ad.get("creative") != {"id": creative_id}:
                raise MetaAdsError(
                    "Meta bundle ad creative identity mismatch", phase="verify_bundle"
                )
            observed = dict(AdCreative(creative_id).api_get(fields=["object_story_spec"]))
            result[f"creative:{locale}"] = observed
            frozen = locales[locale]
            link_data: dict[str, object] = {
                "message": frozen.primary_text,
                "link": landing_page_url,
                "name": frozen.headline,
                "description": frozen.description,
                "call_to_action": {"type": frozen.cta_type},
            }
            if image_hashes and locale in image_hashes:
                link_data["image_hash"] = image_hashes[locale]
            expected_story = {"page_id": config.page_id, "link_data": link_data}
            if observed.get("object_story_spec") != expected_story:
                raise MetaAdsError(
                    "Meta bundle frozen creative identity mismatch", phase="verify_bundle"
                )
    return result


async def get_meta_replacement_bundle_statuses(
    config: MetaConfig,
    campaign_id: str,
    ad_set_id: str,
    ad_ids: Mapping[str, str],
    **identity: object,
) -> dict[str, dict[str, str | int]]:
    """Live-read the complete replacement hierarchy and exact ad-set budget."""
    return await asyncio.to_thread(
        _sync_get_replacement_bundle_statuses,
        config,
        campaign_id,
        ad_set_id,
        ad_ids,
        **identity,
    )


def _resources(ids: dict[str, str]) -> list[tuple[str, object]]:
    try:
        return [
            ("campaign", Campaign(ids["campaign_id"])),
            ("ad_set", AdSet(ids["ad_set_id"])),
            ("ad", Ad(ids["ad_id"])),
        ]
    except KeyError as exc:
        raise MetaAdsError(f"missing Meta resource ID: {exc.args[0]}") from exc


def _sync_get_statuses(config: MetaConfig, ids: dict[str, str]) -> dict[str, dict[str, str]]:
    _ensure_enabled(config)
    _init_api(config)
    return {
        name: dict(resource.api_get(fields=["status", "effective_status"]))
        for name, resource in _resources(ids)
    }


async def get_meta_ad_statuses(
    config: MetaConfig, ids: dict[str, str]
) -> dict[str, dict[str, str]]:
    """Read configured and effective statuses for a Meta ad hierarchy."""
    return await asyncio.to_thread(_sync_get_statuses, config, ids)


def _sync_pause(config: MetaConfig, ids: dict[str, str]) -> dict[str, str]:
    errors: dict[str, str] = {}
    try:
        _ensure_enabled(config)
        _init_api(config)
    except Exception as exc:
        errors["setup"] = _redact_credentials(str(exc), config)
        return errors

    resource_specs = [
        ("ad", Ad, "ad_id"),
        ("ad_set", AdSet, "ad_set_id"),
        ("campaign", Campaign, "campaign_id"),
    ]
    for name, resource_type, id_key in resource_specs:
        if id_key not in ids:
            continue
        try:
            resource = resource_type(ids[id_key])
            resource.api_update(params={"status": "PAUSED"})
        except Exception as exc:  # rollback must continue through every ancestor
            errors[name] = _redact_credentials(str(exc), config)
    return errors


def _redact_credentials(message: str, config: MetaConfig) -> str:
    credentials = sorted(
        {config.app_secret, config.system_user_token} - {""},
        key=len,
        reverse=True,
    )
    for credential in credentials:
        if len(credential) >= 8:
            message = message.replace(credential, "[REDACTED]")
        else:
            message = re.sub(
                rf"(?<![A-Za-z0-9_]){re.escape(credential)}(?![A-Za-z0-9_])",
                "[REDACTED]",
                message,
            )
    return message


async def pause_meta_ad(config: MetaConfig, ids: dict[str, str]) -> dict[str, str]:
    """Best-effort pause in child-to-parent order; return failures by resource."""
    return await asyncio.to_thread(_sync_pause, config, ids)


_MUTABLE_AD_STATUSES = {"ACTIVE", "PAUSED"}


def _validate_resource_id(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _mutation_error(
    message: str,
    config: MetaConfig,
    *,
    phase: str,
    resource_ids: dict[str, str],
    observed_statuses: dict[str, dict[str, str | int]] | None = None,
    sdk_error: Exception | None = None,
) -> MetaAdsError:
    diagnostics = (
        _facebook_error_diagnostics(sdk_error, config)
        if isinstance(sdk_error, FacebookRequestError)
        else {}
    )
    return MetaAdsError(
        _redact_credentials(message, config),
        phase=phase,
        resource_ids=resource_ids,
        observed_statuses=observed_statuses,
        **diagnostics,
    )


def _facebook_error_diagnostics(
    error: FacebookRequestError, config: MetaConfig
) -> dict[str, int | str | None]:
    def safe_value(name: str) -> object:
        accessor = getattr(error, name, None)
        try:
            return accessor() if callable(accessor) else accessor
        except Exception:
            return None

    code = safe_value("api_error_code")
    subcode = safe_value("api_error_subcode")
    status = safe_value("http_status")
    error_type = safe_value("api_error_type")
    return {
        "api_error_code": code if isinstance(code, int) and not isinstance(code, bool) else None,
        "api_error_subcode": (
            subcode if isinstance(subcode, int) and not isinstance(subcode, bool) else None
        ),
        "http_status": (
            status if isinstance(status, int) and not isinstance(status, bool) else None
        ),
        "api_error_type": (
            _redact_credentials(error_type, config) if isinstance(error_type, str) else None
        ),
    }


def _sync_set_ad_status(config: MetaConfig, ad_id: str, status: str) -> dict[str, str]:
    resource_ids = {"ad_id": ad_id}
    phase = "update_ad_status"
    mutation_error: MetaAdsError | None = None
    try:
        _ensure_enabled(config)
        api = _init_api(config)
        ad = Ad(ad_id, api=api)
        ad.api_update(params={"status": status})
        phase = "verify_ad_status"
        observed = dict(ad.api_get(fields=["status", "effective_status"]))
        sanitized = {
            "status": str(observed.get("status", "")),
            "effective_status": str(observed.get("effective_status", "")),
        }
        if sanitized["status"] != status or sanitized["effective_status"] != status:
            raise _mutation_error(
                "Meta ad status verification mismatch",
                config,
                phase=phase,
                resource_ids=resource_ids,
                observed_statuses={"ad": sanitized},
            )
        return sanitized
    except MetaAdsError:
        raise
    except Exception as exc:
        mutation_error = _mutation_error(
            f"Meta ad status mutation failed: {exc}",
            config,
            phase=phase,
            resource_ids=resource_ids,
            sdk_error=exc,
        )
    raise mutation_error


async def set_meta_ad_status(config: MetaConfig, ad_id: str, status: str) -> dict[str, str]:
    """Set one ad's status and return its verified configured/effective state."""
    validated_id = _validate_resource_id(ad_id, "ad_id")
    if not isinstance(status, str) or status not in _MUTABLE_AD_STATUSES:
        raise ValueError("status must be ACTIVE or PAUSED")
    return await asyncio.to_thread(_sync_set_ad_status, config, validated_id, status)


def _normalized_daily_budget(observed: dict) -> dict[str, int]:
    value = observed.get("daily_budget")
    if isinstance(value, bool):
        raise ValueError("invalid daily_budget returned by Meta")
    return {"daily_budget": int(value)}


def _sync_set_adset_daily_budget(config: MetaConfig, ad_set_id: str, cents: int) -> dict[str, int]:
    resource_ids = {"ad_set_id": ad_set_id}
    phase = "update_ad_set_daily_budget"
    mutation_error: MetaAdsError | None = None
    try:
        _ensure_enabled(config)
        api = _init_api(config)
        ad_set = AdSet(ad_set_id, api=api)
        ad_set.api_update(params={"daily_budget": cents})
        phase = "verify_ad_set_daily_budget"
        observed = _normalized_daily_budget(dict(ad_set.api_get(fields=["daily_budget"])))
        if observed["daily_budget"] != cents:
            raise _mutation_error(
                "Meta ad set daily budget verification mismatch",
                config,
                phase=phase,
                resource_ids=resource_ids,
                observed_statuses={"ad_set": observed},
            )
        return observed
    except MetaAdsError:
        raise
    except Exception as exc:
        mutation_error = _mutation_error(
            f"Meta ad set daily budget mutation failed: {exc}",
            config,
            phase=phase,
            resource_ids=resource_ids,
            sdk_error=exc,
        )
    raise mutation_error


async def set_meta_adset_daily_budget(
    config: MetaConfig, ad_set_id: str, cents: int
) -> dict[str, int]:
    """Set one ad set's daily budget in cents and return the verified value."""
    validated_id = _validate_resource_id(ad_set_id, "ad_set_id")
    if isinstance(cents, bool) or not isinstance(cents, int) or cents <= 0:
        raise ValueError("cents must be a positive integer")
    return await asyncio.to_thread(_sync_set_adset_daily_budget, config, validated_id, cents)


def _sync_get_budget_state(
    config: MetaConfig, ids: dict[str, str]
) -> dict[str, dict[str, str | int]]:
    resource_ids = dict(ids)
    mutation_error: MetaAdsError | None = None
    try:
        _ensure_enabled(config)
        api = _init_api(config)
        ad = Ad(ids["ad_id"], api=api)
        ad_set = AdSet(ids["ad_set_id"], api=api)
        ad_state = dict(ad.api_get(fields=["status", "effective_status"]))
        ad_set_raw = dict(ad_set.api_get(fields=["status", "effective_status", "daily_budget"]))
        return {
            "ad": {
                "status": str(ad_state.get("status", "")),
                "effective_status": str(ad_state.get("effective_status", "")),
            },
            "ad_set": {
                "status": str(ad_set_raw.get("status", "")),
                "effective_status": str(ad_set_raw.get("effective_status", "")),
                **_normalized_daily_budget(ad_set_raw),
            },
        }
    except Exception as exc:
        mutation_error = _mutation_error(
            f"Meta budget state read failed: {exc}",
            config,
            phase="get_budget_state",
            resource_ids=resource_ids,
            sdk_error=exc,
        )
    raise mutation_error


async def get_meta_budget_state(
    config: MetaConfig, ids: dict[str, str]
) -> dict[str, dict[str, str | int]]:
    """Read ad status and its ad set status/current daily budget."""
    if not isinstance(ids, dict):
        raise ValueError("ids must contain ad_id and ad_set_id")
    validated = {
        "ad_id": _validate_resource_id(ids.get("ad_id"), "ad_id"),
        "ad_set_id": _validate_resource_id(ids.get("ad_set_id"), "ad_set_id"),
    }
    return await asyncio.to_thread(_sync_get_budget_state, config, validated)


def _sync_observe_best_effort(config: MetaConfig, ids: dict[str, str]) -> dict[str, dict[str, str]]:
    try:
        return _sync_get_statuses(config, ids)
    except Exception:
        return {}


def _validate_statuses(statuses: dict[str, dict[str, str]]) -> None:
    accepted_effective_statuses = {"ACTIVE", "IN_PROCESS", "PENDING_REVIEW"}
    for resource_name, status in statuses.items():
        configured = status.get("status")
        effective = status.get("effective_status")
        if configured != "ACTIVE" or effective not in accepted_effective_statuses:
            raise MetaAdsError(f"Meta {resource_name} did not reach an active or review state")


def _sync_activate(config: MetaConfig, ids: dict[str, str]) -> MetaActivationResult:
    phase = "setup"
    activation_error: MetaAdsError | None = None
    try:
        _ensure_enabled(config)
        _init_api(config)
        for name, resource in _resources(ids):
            phase = f"activate_{name}"
            resource.api_update(params={"status": "ACTIVE"})
        phase = "verify_statuses"
        statuses = _sync_get_statuses(config, ids)
        _validate_statuses(statuses)
        return MetaActivationResult(
            campaign=statuses["campaign"],
            ad_set=statuses["ad_set"],
            ad=statuses["ad"],
        )
    except Exception:
        observed_statuses = _sync_observe_best_effort(config, ids)
        rollback_errors = _sync_pause(config, ids)
        activation_error = MetaAdsError(
            "Meta activation failed",
            phase=phase,
            resource_ids=dict(ids),
            observed_statuses=observed_statuses,
            rollback_errors=rollback_errors,
        )
    raise activation_error from None


async def activate_meta_ad(config: MetaConfig, ids: dict[str, str]) -> MetaActivationResult:
    """Activate parent-to-child, verify status, and roll back on failure."""
    return await asyncio.to_thread(_sync_activate, config, ids)
