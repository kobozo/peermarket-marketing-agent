"""Meta Marketing API connector — creates paused ads from approved drafts.

We always create in PAUSED state. The founder activates manually in
Ads Manager. This keeps the agent in 'propose' autonomy mode per the
spec's autonomy graduation rules — paid spend never happens without
explicit human action.
"""

import asyncio
import base64
from dataclasses import dataclass
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


@dataclass(frozen=True)
class MetaAdResult:
    ad_id: str
    ad_set_id: str
    campaign_id: str
    creative_id: str
    ads_manager_url: str
    status: str


class MetaAdsDisabled(RuntimeError):
    """Meta connector cannot operate — credentials missing."""


class MetaAdsError(RuntimeError):
    """Any Meta API failure."""


# Targeting templates per audience profile. Belgium-only, NL+FR.
# These mirror the AUDIENCE_PROFILES in prompts/meta_ad_creative.py but
# in Meta's actual targeting-spec JSON shape.
_TARGETING_TEMPLATES = {
    "declutterers": {
        "age_min": 28,
        "age_max": 55,
        "geo_locations": {"countries": ["BE"]},
        "locales": [5, 24],  # Dutch (5), French (24) — Meta locale IDs for Belgium
        "publisher_platforms": ["facebook", "instagram"],
        "facebook_positions": ["feed", "story"],
        "instagram_positions": ["stream", "story", "reels"],
    },
    "trust_conscious_locals": {
        "age_min": 35,
        "age_max": 65,
        "geo_locations": {"countries": ["BE"]},
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
        }.items()
        if not v
    ]
    if missing:
        raise MetaAdsDisabled(
            f"Meta connector disabled — missing credentials: {missing}. "
            "Set META_APP_ID, META_APP_SECRET, META_SYSTEM_USER_TOKEN, META_AD_ACCOUNT_ID."
        )


def _init_api(config: MetaConfig) -> None:
    """Initialize the global FacebookAdsApi singleton. Safe to call repeatedly."""
    FacebookAdsApi.init(
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

    try:
        # 1) Campaign — paused, traffic objective, no special ad categories
        campaign = account.create_campaign(
            params={
                Campaign.Field.name: f"{name} — campaign",
                Campaign.Field.objective: "OUTCOME_TRAFFIC",
                Campaign.Field.status: Campaign.Status.paused,
                Campaign.Field.special_ad_categories: [],
            },
            fields=[Campaign.Field.id],
        )
        campaign_id = campaign["id"]
        log.info("meta_ads.campaign_created", campaign_id=campaign_id)

        # 2) AdSet — targeting + budget in cents
        adset = account.create_ad_set(
            params={
                AdSet.Field.name: f"{name} — adset",
                AdSet.Field.campaign_id: campaign_id,
                AdSet.Field.daily_budget: daily_budget_eur * 100,
                AdSet.Field.billing_event: "LINK_CLICKS",
                AdSet.Field.optimization_goal: "LINK_CLICKS",
                AdSet.Field.bid_strategy: "LOWEST_COST_WITHOUT_CAP",
                AdSet.Field.targeting: _TARGETING_TEMPLATES[audience_profile_key],
                AdSet.Field.status: AdSet.Status.paused,
            },
            fields=[AdSet.Field.id],
        )
        adset_id = adset["id"]
        log.info("meta_ads.adset_created", adset_id=adset_id)

        # 3) Image upload (optional)
        image_hash: str | None = None
        if image_bytes:
            b64 = base64.b64encode(image_bytes).decode("ascii")
            image = account.create_ad_image(
                params={"bytes": b64},
                fields=["hash"],
            )
            image_hash = image["hash"]
            log.info("meta_ads.image_uploaded", image_hash=image_hash)

        # 4) Creative
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
                    "link_data": link_data,
                },
            },
            fields=[AdCreative.Field.id],
        )
        creative_id = creative["id"]
        log.info("meta_ads.creative_created", creative_id=creative_id)

        # 5) Ad
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
        raise MetaAdsError(f"Meta API error: {e.api_error_message() or e}") from e


async def create_paused_ad(
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
