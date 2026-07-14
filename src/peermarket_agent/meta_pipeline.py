"""Approved meta_ad_creative draft → paused Meta ad with brand-framed screenshot."""

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from peermarket_agent.config import Settings
from peermarket_agent.meta_ads import (
    MetaAdsDisabled,
    MetaAdsError,
    MetaConfig,
    create_meta_ad_paused,
)
from peermarket_agent.nano_banana import (
    ImageEditDisabled,
    ImageEditError,
    edit_image,
)
from peermarket_agent.screenshots import ScreenshotError, screenshot_url
from peermarket_agent.slack_notifier import SlackNotifier

log = structlog.get_logger(__name__)

_BRAND_FRAME_PROMPT = (
    "Add a PeerMarket brand frame around this screenshot:\n"
    "- Subtle 20px border using the PeerMarket green (#1a5d3a)\n"
    "- Bottom strip with the wordmark 'peermarket.eu' in clean sans-serif\n"
    "- Small 'Verified Identity' badge in the top-right corner\n"
    "Keep the original screenshot content fully intact and visible. Do not "
    "synthesize people, products, or transactions."
)
_LANDING_PAGE = "https://peermarket.eu/"


async def _fetch_meta_draft_metadata(engine: AsyncEngine, draft_id: int) -> dict | None:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT at.name, d.metadata "
                    "FROM drafts d JOIN action_types at ON at.id = d.action_type_id "
                    "WHERE d.id = :id"
                ),
                {"id": draft_id},
            )
        ).fetchone()
    if row is None or row[0] != "meta_ad_creative":
        return None
    return row[1] or {}


def _build_landing_url(draft_id: int) -> str:
    return (
        f"{_LANDING_PAGE}"
        f"?utm_source=meta&utm_medium=paid&utm_campaign=phase2"
        f"&utm_content=draft-{draft_id}"
    )


async def process_approved_meta_draft(
    *,
    engine: AsyncEngine,
    draft_id: int,
    settings: Settings,
    notifier: SlackNotifier,
) -> None:
    """End-to-end pipeline. Best-effort. Never raises."""
    log.info("meta_pipeline.start", draft_id=draft_id)
    metadata = await _fetch_meta_draft_metadata(engine, draft_id)
    if metadata is None:
        log.warning("meta_pipeline.draft_missing_or_wrong_type", draft_id=draft_id)
        return
    if not metadata:
        await notifier.notify_founder(
            f"⚠️ Couldn't push draft #{draft_id} to Meta — it was created before "
            "we added structured metadata. Regenerate it and re-approve."
        )
        return

    try:
        screenshot_bytes = await screenshot_url(
            _LANDING_PAGE, viewport_width=1080, viewport_height=1080
        )
    except ScreenshotError as e:
        log.exception("meta_pipeline.screenshot_failed", draft_id=draft_id)
        await notifier.notify_founder(
            f"⚠️ Approved draft #{draft_id} but couldn't screenshot peermarket.eu: {e}. "
            "Draft is still approved in DB — manually copy into Ads Manager."
        )
        return

    image_bytes = screenshot_bytes
    try:
        image_bytes = await edit_image(
            api_key=settings.gemini_api_key,
            image_bytes=screenshot_bytes,
            prompt=_BRAND_FRAME_PROMPT,
        )
    except ImageEditDisabled:
        log.info("meta_pipeline.nano_banana_disabled", draft_id=draft_id)
    except ImageEditError as e:
        log.exception("meta_pipeline.image_edit_failed", draft_id=draft_id)
        await notifier.notify_founder(
            f"⚠️ Approved draft #{draft_id}, screenshot OK, but image-edit failed: {e}. "
            "Falling back to raw screenshot, continuing with Meta push."
        )

    meta_config = MetaConfig(
        app_id=settings.meta_app_id,
        app_secret=settings.meta_app_secret,
        system_user_token=settings.meta_system_user_token,
        ad_account_id=settings.meta_ad_account_id,
    )
    try:
        result = await create_meta_ad_paused(
            config=meta_config,
            name=f"PeerMarket draft #{draft_id}",
            primary_text=metadata["primary_text"],
            headline=metadata["headline"],
            description=metadata["description"],
            cta_type=metadata["cta_type"],
            landing_page_url=_build_landing_url(draft_id),
            image_bytes=image_bytes,
            audience_profile_key=metadata["audience_profile_key"],
            daily_budget_eur=metadata["suggested_daily_budget_eur"],
        )
    except MetaAdsDisabled as e:
        await notifier.notify_founder(
            f"⚠️ Approved draft #{draft_id}, but Meta connector isn't configured: {e}. "
            "Set the META_* secrets + redeploy to enable auto-push."
        )
        return
    except MetaAdsError as e:
        log.exception("meta_pipeline.meta_create_failed", draft_id=draft_id)
        await notifier.notify_founder(
            f"⚠️ Approved draft #{draft_id}, but Meta API rejected the push: {e}. "
            "Draft is still approved in DB — try manually."
        )
        return

    await notifier.notify_founder(
        f"📣 *Created paused Meta ad for draft #{draft_id}*\n"
        f"Open in Ads Manager: {result.ads_manager_url}\n"
        f"All resources PAUSED — activate when ready. "
        f"Audience: {metadata['audience_profile_key']} · "
        f"Budget: €{metadata['suggested_daily_budget_eur']}/day"
    )
    log.info(
        "meta_pipeline.success",
        draft_id=draft_id,
        ad_id=result.ad_id,
        campaign_id=result.campaign_id,
    )
