"""Meta Marketing API connector for creating and activating approved ads."""

import asyncio
import base64
import hashlib
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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
    local_image_sha256s: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class MetaHookExperimentResult:
    campaign_id: str
    ad_set_id: str
    variants: Mapping[str, MetaReplacementBundleResult]
    status: str = "PAUSED"


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

    safe_effective = {
        "PAUSED",
        "CAMPAIGN_PAUSED",
        "ADSET_PAUSED",
        "PENDING_REVIEW",
        "IN_PROCESS",
        "PREAPPROVED",
        "PENDING_BILLING_INFO",
    }

    def existing(
        getter_name: str,
        resource_name: str,
        *,
        fields: list[str],
        expected: Mapping[str, object],
        id_field: str = "id",
        hierarchy_factory: Callable[[str], list[tuple[str, type, str]]] | None = None,
        require_paused: bool = False,
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
        if require_paused:
            hierarchy = hierarchy_factory(value) if hierarchy_factory else []
            observed = {
                "status": candidate.get("status"),
                "effective_status": candidate.get("effective_status"),
            }
            if observed["status"] != "PAUSED" or observed["effective_status"] not in safe_effective:
                pause_errors: dict[str, str] = {}
                reread: dict[str, dict[str, object]] = {}
                for label, resource_type, resource_id in hierarchy:
                    try:
                        resource_type(resource_id).api_update(params={"status": "PAUSED"})
                    except Exception as exc:
                        pause_errors[label] = _redact_credentials(str(exc), config)
                for label, resource_type, resource_id in hierarchy:
                    try:
                        reread[label] = dict(
                            resource_type(resource_id).api_get(
                                fields=["status", "effective_status"]
                            )
                        )
                    except Exception as exc:
                        pause_errors[f"{label}_reread"] = _redact_credentials(str(exc), config)
                candidate_state = reread.get(hierarchy[0][0], {}) if hierarchy else {}
                if (
                    pause_errors
                    or candidate_state.get("status") != "PAUSED"
                    or candidate_state.get("effective_status") not in safe_effective
                    or any(
                        state.get("status") != "PAUSED"
                        or state.get("effective_status") not in safe_effective
                        for state in reread.values()
                    )
                ):
                    raise MetaAdsError(
                        "Meta bundle identity could not be made safely paused",
                        phase="reconcile_bundle_identity",
                        resource_ids={label: rid for label, _, rid in hierarchy},
                        observed_statuses=reread,
                        rollback_errors=pause_errors,
                    )
        elif candidate.get("status") is not None and (
            candidate.get("status") != "PAUSED"
            or candidate.get("effective_status") not in safe_effective
        ):
            raise MetaAdsError(
                "Meta bundle identity is not safely paused",
                phase="reconcile_bundle_identity",
                resource_ids={id_field: value},
                observed_statuses={
                    "candidate": {
                        "status": str(candidate.get("status")),
                        "effective_status": str(candidate.get("effective_status")),
                    }
                },
            )
        return value

    if "campaign_id" not in progress:
        resource_name = f"{name} — campaign"
        account_id = config.ad_account_id.removeprefix("act_")
        if found := existing(
            "get_campaigns",
            resource_name,
            fields=[
                "id",
                "name",
                "objective",
                "special_ad_categories",
                "account_id",
                "status",
                "effective_status",
            ],
            expected={
                "objective": "OUTCOME_TRAFFIC",
                "special_ad_categories": [],
                "account_id": account_id,
            },
            hierarchy_factory=lambda candidate_id: [("campaign", Campaign, candidate_id)],
            require_paused=True,
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
                "status",
                "effective_status",
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
            hierarchy_factory=lambda candidate_id: [
                ("ad_set", AdSet, candidate_id),
                ("campaign", Campaign, progress["campaign_id"]),
            ],
            require_paused=True,
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
        local_sha256 = hashlib.sha256(creative.image_bytes).hexdigest()
        expected_meta_hash = hashlib.md5(creative.image_bytes).hexdigest()  # noqa: S324 -- Meta content identity
        image_name = f"{name} {locale} image {local_sha256[:20]}"
        if found := existing(
            "get_ad_images",
            image_name,
            fields=["hash", "name"],
            expected={"hash": expected_meta_hash},
            id_field="hash",
        ):
            return image_key, found
        image = account.create_ad_image(
            params={
                "bytes": base64.b64encode(creative.image_bytes).decode("ascii"),
                "name": image_name,
            },
            fields=["hash"],
        )
        if image.get("hash") != expected_meta_hash:
            raise MetaAdsError(
                "Meta image upload returned unexpected content hash",
                phase="verify_bundle_image_hash",
                observed_statuses={"image": {"hash": str(image.get("hash"))}},
            )
        return image_key, expected_meta_hash
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
            fields=["id", "name", "object_story_spec", "status", "effective_status"],
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
        fields=["id", "name", "adset_id", "creative", "status", "effective_status"],
        expected={"adset_id": progress["ad_set_id"], "creative": {"id": progress[creative_key]}},
        hierarchy_factory=lambda candidate_id: [
            ("ad", Ad, candidate_id),
            ("ad_set", AdSet, progress["ad_set_id"]),
            ("campaign", Campaign, progress["campaign_id"]),
        ],
        require_paused=True,
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
    landing_page_url: str | Mapping[str, str],
    audience_profile_key: str,
    daily_budget_eur: int,
    progress: Mapping[str, str] | None = None,
    persist_progress: Callable[[str, str], Awaitable[None]],
) -> MetaReplacementBundleResult:
    """Idempotently create one paused campaign/adset and exactly NL/FR/EN ads."""
    if set(locales) != {"NL", "FR", "EN"}:
        raise ValueError("Meta replacement bundle requires exact NL/FR/EN locales")
    current = dict(progress or {})
    # Fence the entire retry before any lookup or create. A newly captured/edited
    # image must never be attached to durable IDs created for different bytes.
    for locale, child in locales.items():
        if not child.image_bytes:
            continue
        expected_sha256 = hashlib.sha256(child.image_bytes).hexdigest()
        expected_meta_hash = hashlib.md5(child.image_bytes).hexdigest()  # noqa: S324
        stored_sha256 = current.get(f"local_image_sha256:{locale}")
        stored_meta_hash = current.get(f"image_hash:{locale}")
        if (stored_sha256 is not None and stored_sha256 != expected_sha256) or (
            stored_meta_hash is not None and stored_meta_hash != expected_meta_hash
        ):
            raise MetaAdsError(
                "durable progress does not match current frozen image bytes",
                phase="validate_bundle_image_identity",
            )
    for locale in (None, None, "NL", "FR", "EN"):
        child = locales.get(locale) if locale else None
        while (locale is None and ("campaign_id" not in current or "ad_set_id" not in current)) or (
            locale is not None and f"ad_id:{locale}" not in current
        ):
            try:
                if locale is not None and child is not None and child.image_bytes:
                    sha_key = f"local_image_sha256:{locale}"
                    sha_value = hashlib.sha256(child.image_bytes).hexdigest()
                    if sha_key not in current:
                        current[sha_key] = sha_value
                        await persist_progress(sha_key, sha_value)
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
                    landing_page_url=(
                        landing_page_url[locale]
                        if locale is not None and isinstance(landing_page_url, Mapping)
                        else next(iter(landing_page_url.values()))
                        if isinstance(landing_page_url, Mapping)
                        else landing_page_url
                    ),
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
        local_image_sha256s={
            locale: current[f"local_image_sha256:{locale}"]
            for locale in ("NL", "FR", "EN")
            if f"local_image_sha256:{locale}" in current
        },
    )


async def create_meta_hook_experiment_bundles_paused(
    *,
    config: MetaConfig,
    experiment_id: str,
    variants: Mapping[str, Mapping[str, MetaBundleLocale]],
    landing_page_url: str,
    audience_profile_key: str,
    daily_budget_eur: int,
    progress: Mapping[str, str] | None = None,
    persist_progress: Callable[[str, str], Awaitable[None]],
) -> MetaHookExperimentResult:
    """Create three paused hook variants under one fenced campaign/ad set."""
    expected = tuple(f"{experiment_id}:{number:02}" for number in (1, 2, 3))
    if tuple(variants) != expected or any(
        set(bundle) != {"NL", "FR", "EN"} for bundle in variants.values()
    ):
        raise ValueError(
            "hook experiment requires ordered :01/:02/:03 variants with exact NL/FR/EN"
        )
    durable = dict(progress or {})
    results: dict[str, MetaReplacementBundleResult] = {}
    shared = {key: durable[key] for key in ("campaign_id", "ad_set_id") if key in durable}
    for variant_id in expected:
        prefix = f"variant:{variant_id}:"
        local = dict(shared)
        local.update(
            {
                key.removeprefix(prefix): value
                for key, value in durable.items()
                if key.startswith(prefix)
            }
        )

        async def persist(key: str, value: str, *, _prefix: str = prefix) -> None:
            durable_key = key if key in {"campaign_id", "ad_set_id"} else _prefix + key
            existing = durable.get(durable_key)
            if existing is not None and existing != value:
                raise MetaAdsError(
                    "durable hook experiment identity drift", phase="persist_hook_bundle"
                )
            durable[durable_key] = value
            await persist_progress(durable_key, value)

        result = await create_meta_replacement_bundle_paused(
            config=config,
            name=f"{experiment_id} {variant_id}",
            locales=variants[variant_id],
            landing_page_url=hook_variant_locale_urls(landing_page_url, variant_id),
            audience_profile_key=audience_profile_key,
            daily_budget_eur=daily_budget_eur,
            progress=local,
            persist_progress=persist,
        )
        if shared and (
            result.campaign_id != shared["campaign_id"] or result.ad_set_id != shared["ad_set_id"]
        ):
            raise MetaAdsError("hook variant parent identity drift", phase="verify_hook_bundle")
        shared = {"campaign_id": result.campaign_id, "ad_set_id": result.ad_set_id}
        results[variant_id] = result
    ad_ids = [item for result in results.values() for item in result.ad_ids.values()]
    creative_ids = [item for result in results.values() for item in result.creative_ids.values()]
    if len(set(ad_ids)) != 9 or len(set(creative_ids)) != 9:
        raise MetaAdsError("hook variants reused child identity", phase="verify_hook_bundle")
    return MetaHookExperimentResult(shared["campaign_id"], shared["ad_set_id"], results)


def _with_utm_content(url: str, identity: str) -> str:
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query) if key != "utm_content"]
    query.append(("utm_content", identity))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def hook_variant_locale_urls(base_url: str, variant_id: str) -> dict[str, str]:
    """Return the frozen destination identity for each locale ad."""
    return {
        locale: _with_utm_content(base_url, f"{variant_id}:{locale}")
        for locale in ("NL", "FR", "EN")
    }


def _sync_get_replacement_bundle_statuses(
    config: MetaConfig,
    campaign_id: str,
    ad_set_id: str,
    ad_ids: Mapping[str, str],
    *,
    creative_ids: Mapping[str, str] | None = None,
    landing_page_url: str | Mapping[str, str] | None = None,
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
                "link": (
                    landing_page_url[locale]
                    if isinstance(landing_page_url, Mapping)
                    else landing_page_url
                ),
                "name": frozen.headline,
                "description": frozen.description,
                "call_to_action": {"type": frozen.cta_type},
            }
            if frozen.image_bytes:
                expected_image_hash = hashlib.md5(frozen.image_bytes).hexdigest()  # noqa: S324
                if image_hashes and image_hashes.get(locale) != expected_image_hash:
                    raise MetaAdsError(
                        "Meta bundle frozen image hash mismatch", phase="verify_bundle"
                    )
                link_data["image_hash"] = expected_image_hash
            elif image_hashes and locale in image_hashes:
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


def _sync_pause_replacement_bundle(
    config: MetaConfig,
    campaign_id: str,
    ad_set_id: str,
    ad_ids: Mapping[str, str],
) -> dict[str, object]:
    """Fence a newly-created replacement hierarchy, then retain live evidence."""
    _ensure_enabled(config)
    _init_api(config)
    errors: dict[str, str] = {}
    resources: list[tuple[str, object]] = [
        *[(f"ad:{locale}", Ad(ad_id)) for locale, ad_id in ad_ids.items()],
        ("ad_set", AdSet(ad_set_id)),
        ("campaign", Campaign(campaign_id)),
    ]
    for label, resource in resources:
        try:
            resource.api_update(params={"status": "PAUSED"})
        except Exception as exc:
            errors[label] = _redact_credentials(str(exc), config)
    observed: dict[str, dict[str, object]] = {}
    for label, resource in resources:
        try:
            observed[label] = dict(resource.api_get(fields=["status", "effective_status"]))
        except Exception as exc:
            errors[f"{label}:reread"] = _redact_credentials(str(exc), config)
    return {"observed": observed, "pause_errors": errors}


async def pause_meta_replacement_bundle(
    config: MetaConfig,
    campaign_id: str,
    ad_set_id: str,
    ad_ids: Mapping[str, str],
) -> dict[str, object]:
    """Best-effort child-to-parent fence for only the new replacement hierarchy."""
    try:
        return await asyncio.to_thread(
            _sync_pause_replacement_bundle, config, campaign_id, ad_set_id, ad_ids
        )
    except Exception as exc:
        return {
            "observed": {},
            "pause_errors": {"setup": _redact_credentials(str(exc), config)},
        }


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


def _sync_set_meta_resource_status(
    config: MetaConfig, resource_kind: str, resource_id: str, status: str
) -> dict[str, str]:
    _ensure_enabled(config)
    api = _init_api(config)
    resource_type = {"campaign": Campaign, "ad_set": AdSet}[resource_kind]
    resource = resource_type(resource_id, api=api)
    resource.api_update(params={"status": status})
    return dict(resource.api_get(fields=["status", "effective_status"]))


async def set_meta_resource_status(
    config: MetaConfig, resource_kind: str, resource_id: str, status: str
) -> dict[str, str]:
    """Set one campaign or ad-set status through one SDK mutation."""
    if resource_kind not in {"campaign", "ad_set"}:
        raise ValueError("resource_kind must be campaign or ad_set")
    validated_id = _validate_resource_id(resource_id, f"{resource_kind}_id")
    if status not in {"ACTIVE", "PAUSED"}:
        raise ValueError("status must be ACTIVE or PAUSED")
    return await asyncio.to_thread(
        _sync_set_meta_resource_status, config, resource_kind, validated_id, status
    )


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


def _sync_get_allocation_state(
    config: MetaConfig, campaign_id: str, ad_set_id: str, ad_id: str
) -> dict[str, object]:
    resource_ids = {
        "campaign_id": campaign_id,
        "ad_set_id": ad_set_id,
        "ad_id": ad_id,
    }
    try:
        _ensure_enabled(config)
        api = _init_api(config)
        ad = dict(Ad(ad_id, api=api).api_get(fields=["status", "effective_status", "adset_id"]))
        ad_set = dict(
            AdSet(ad_set_id, api=api).api_get(
                fields=["status", "effective_status", "daily_budget", "campaign_id"]
            )
        )
        if str(ad.get("adset_id")) != ad_set_id or str(ad_set.get("campaign_id")) != campaign_id:
            raise _mutation_error(
                "Meta allocation hierarchy mismatch",
                config,
                phase="get_allocation_state",
                resource_ids=resource_ids,
            )
        return {
            **resource_ids,
            "ad": {
                "status": str(ad.get("status", "")),
                "effective_status": str(ad.get("effective_status", "")),
                "ad_set_id": str(ad.get("adset_id", "")),
            },
            "ad_set": {
                "status": str(ad_set.get("status", "")),
                "effective_status": str(ad_set.get("effective_status", "")),
                "campaign_id": str(ad_set.get("campaign_id", "")),
                **_normalized_daily_budget(ad_set),
            },
        }
    except MetaAdsError:
        raise
    except Exception as exc:
        raise _mutation_error(
            f"Meta allocation state read failed: {exc}",
            config,
            phase="get_allocation_state",
            resource_ids=resource_ids,
            sdk_error=exc,
        ) from None


async def get_meta_allocation_state(
    config: MetaConfig, campaign_id: str, ad_set_id: str, ad_id: str
) -> dict[str, object]:
    """Read and verify exact ad -> ad set -> campaign ownership and budget."""
    values = [
        _validate_resource_id(campaign_id, "campaign_id"),
        _validate_resource_id(ad_set_id, "ad_set_id"),
        _validate_resource_id(ad_id, "ad_id"),
    ]
    return await asyncio.to_thread(_sync_get_allocation_state, config, *values)


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
