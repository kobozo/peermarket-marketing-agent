"""Approved Meta draft to durable, activated publication."""

import json

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from peermarket_agent.config import Settings
from peermarket_agent.meta_ads import (
    MetaAdsDisabled,
    MetaAdsError,
    MetaConfig,
    activate_meta_ad,
    create_meta_ad_paused,
    pause_meta_ad,
)
from peermarket_agent.nano_banana import (
    ImageEditDisabled,
    ImageEditError,
    edit_image,
)
from peermarket_agent.publications import (
    MetaPublication,
    get_meta_publication,
    upsert_meta_publication,
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


async def _fetch_meta_draft(engine: AsyncEngine, draft_id: int) -> tuple[str, dict] | None:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT at.name, d.status, d.metadata "
                    "FROM drafts d JOIN action_types at ON at.id = d.action_type_id "
                    "WHERE d.id = :id"
                ),
                {"id": draft_id},
            )
        ).fetchone()
    if row is None or row[0] != "meta_ad_creative":
        return None
    return row[1], row[2] or {}


def _failure_details(error: MetaAdsError, default_phase: str) -> dict:
    return {
        "phase": error.phase or default_phase,
        "rollback_complete": not error.rollback_errors,
        "rollback_errors": error.rollback_errors,
    }


async def _record_failure(
    engine: AsyncEngine,
    *,
    draft_id: int,
    phase: str,
    budget_cents: int,
    error: Exception,
    ids: dict | None = None,
    statuses: dict | None = None,
    ads_manager_url: str | None = None,
) -> dict:
    details = (
        _failure_details(error, phase)
        if isinstance(error, MetaAdsError)
        else {"phase": phase, "rollback_complete": True, "rollback_errors": {}}
    )
    await upsert_meta_publication(
        engine,
        MetaPublication(
            draft_id=draft_id,
            state="failed",
            external_ids=ids or getattr(error, "resource_ids", {}),
            external_statuses=statuses or getattr(error, "observed_statuses", {}),
            failure=details,
            approved_budget_cents=budget_cents,
            ads_manager_url=ads_manager_url,
        ),
    )
    return details


async def _mark_published(
    engine: AsyncEngine, draft_id: int, statuses: dict[str, dict[str, str]]
) -> None:
    """Commit verified publication state and draft transition atomically."""
    async with engine.begin() as connection:
        result = await connection.execute(
            text(
                "UPDATE publications SET state = 'active', "
                "external_statuses = COALESCE(external_statuses, '{}'::JSONB) "
                "|| CAST(:statuses AS JSONB), failure = NULL, updated_at = NOW() "
                "WHERE draft_id = :draft_id"
            ),
            {"draft_id": draft_id, "statuses": json.dumps(statuses)},
        )
        if result.rowcount != 1:
            raise RuntimeError("publication disappeared before finalization")
        draft_result = await connection.execute(
            text(
                "UPDATE drafts SET status = 'published' "
                "WHERE id = :draft_id AND status = 'approved'"
            ),
            {"draft_id": draft_id},
        )
        if draft_result.rowcount != 1:
            raise RuntimeError("draft was not approved during publication finalization")


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
    """Serialize one draft's lifecycle so concurrent retries cannot duplicate it."""
    async with engine.begin() as lock_connection:
        await lock_connection.execute(
            text(
                "SELECT pg_advisory_xact_lock("
                "hashtext('peermarket_meta_pipeline'), hashint8(:draft_id))"
            ),
            {"draft_id": draft_id},
        )
        await _process_approved_meta_draft(
            engine=engine,
            draft_id=draft_id,
            settings=settings,
            notifier=notifier,
        )


async def _process_approved_meta_draft(
    *,
    engine: AsyncEngine,
    draft_id: int,
    settings: Settings,
    notifier: SlackNotifier,
) -> None:
    """End-to-end pipeline. Best-effort. Never raises connector failures."""
    log.info("meta_pipeline.start", draft_id=draft_id)
    draft = await _fetch_meta_draft(engine, draft_id)
    if draft is None:
        log.warning("meta_pipeline.draft_missing_or_wrong_type", draft_id=draft_id)
        return
    draft_status, metadata = draft
    if draft_status == "published":
        log.info("meta_pipeline.already_published", draft_id=draft_id)
        return
    if not metadata:
        await notifier.notify_founder(
            f"⚠️ Couldn't push draft #{draft_id} to Meta — it was created before "
            "we added structured metadata. Regenerate it and re-approve."
        )
        return
    if draft_status != "approved":
        log.warning("meta_pipeline.draft_not_approved", draft_id=draft_id, status=draft_status)
        return

    metadata_budget_cents = int(metadata["suggested_daily_budget_eur"]) * 100
    publication = await get_meta_publication(engine, draft_id)
    budget_cents = (
        publication.approved_budget_cents
        if publication is not None and publication.approved_budget_cents is not None
        else metadata_budget_cents
    )
    if publication is None:
        await upsert_meta_publication(
            engine,
            MetaPublication(
                draft_id=draft_id,
                state="creating",
                approved_budget_cents=budget_cents,
            ),
        )
        publication = await get_meta_publication(engine, draft_id)
    elif publication.approved_budget_cents is None:
        await upsert_meta_publication(
            engine,
            MetaPublication(
                draft_id=draft_id,
                state=publication.state,
                approved_budget_cents=budget_cents,
            ),
        )

    existing_ids = publication.external_ids if publication else {}
    if existing_ids:
        required_ids = {"campaign_id", "ad_set_id", "ad_id"}
        if not required_ids.issubset(existing_ids):
            details = publication.failure or {
                "phase": "reconcile_ids",
                "rollback_complete": False,
                "rollback_errors": {"reconcile": "stored Meta hierarchy is incomplete"},
            }
            if publication.failure is None:
                await upsert_meta_publication(
                    engine,
                    MetaPublication(
                        draft_id=draft_id,
                        state="failed",
                        external_ids=existing_ids,
                        failure=details,
                        approved_budget_cents=budget_cents,
                    ),
                )
            await notifier.notify_founder(
                f"⚠️ Draft #{draft_id} has incomplete stored Meta IDs; no duplicate "
                f"resources were created. Rollback complete: "
                f"{'yes' if details['rollback_complete'] else 'no'}."
            )
            return
        ids = existing_ids
        ads_manager_url = publication.ads_manager_url
    else:
        try:
            screenshot_bytes = await screenshot_url(
                _LANDING_PAGE, viewport_width=1080, viewport_height=1080
            )
        except ScreenshotError as e:
            log.exception("meta_pipeline.screenshot_failed", draft_id=draft_id)
            await _record_failure(
                engine,
                draft_id=draft_id,
                phase="screenshot",
                budget_cents=budget_cents,
                error=e,
            )
            await notifier.notify_founder(
                f"⚠️ Approved draft #{draft_id} but couldn't screenshot peermarket.eu: {e}. "
                "Draft remains approved; retry after fixing screenshot capture."
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
                daily_budget_eur=budget_cents // 100,
            )
        except MetaAdsDisabled as e:
            log.exception("meta_pipeline.meta_create_disabled", draft_id=draft_id)
            await _record_failure(
                engine,
                draft_id=draft_id,
                phase="create",
                budget_cents=budget_cents,
                error=e,
            )
            await notifier.notify_founder(
                f"⚠️ Approved draft #{draft_id}, but Meta connector isn't configured: {e}. "
                "Set the META_* secrets + redeploy; draft remains approved."
            )
            return
        except MetaAdsError as e:
            log.exception("meta_pipeline.meta_create_failed", draft_id=draft_id)
            await _record_failure(
                engine,
                draft_id=draft_id,
                phase="create",
                budget_cents=budget_cents,
                error=e,
            )
            await notifier.notify_founder(
                f"⚠️ Approved draft #{draft_id}, but Meta creation failed: {e}. "
                "Draft remains approved; no automatic duplicate retry was attempted."
            )
            return
        ids = {
            "campaign_id": result.campaign_id,
            "ad_set_id": result.ad_set_id,
            "creative_id": result.creative_id,
            "ad_id": result.ad_id,
        }
        ads_manager_url = result.ads_manager_url
        await upsert_meta_publication(
            engine,
            MetaPublication(
                draft_id=draft_id,
                state="created",
                external_ids=ids,
                approved_budget_cents=budget_cents,
                ads_manager_url=ads_manager_url,
            ),
        )

    meta_config = MetaConfig(
        app_id=settings.meta_app_id,
        app_secret=settings.meta_app_secret,
        system_user_token=settings.meta_system_user_token,
        ad_account_id=settings.meta_ad_account_id,
    )
    try:
        activation = await activate_meta_ad(meta_config, ids)
    except (MetaAdsDisabled, MetaAdsError) as e:
        log.exception("meta_pipeline.activation_failed", draft_id=draft_id)
        details = await _record_failure(
            engine,
            draft_id=draft_id,
            phase="activate",
            budget_cents=budget_cents,
            error=e,
            ids=ids,
            ads_manager_url=ads_manager_url,
        )
        rollback_state = (
            "Rollback complete" if details["rollback_complete"] else "Rollback incomplete"
        )
        await notifier.notify_founder(
            f"⚠️ Meta activation failed for draft #{draft_id} during {details['phase']}. "
            f"{rollback_state}; stored IDs retained for reconciliation. Draft remains approved."
        )
        return

    statuses = {
        "campaign": activation.campaign,
        "ad_set": activation.ad_set,
        "ad": activation.ad,
    }
    try:
        await _mark_published(engine, draft_id, statuses)
    except Exception:
        log.exception("meta_pipeline.finalize_failed", draft_id=draft_id)
        rollback_errors = await pause_meta_ad(meta_config, ids)
        failure = MetaAdsError(
            "database finalization failed after Meta activation",
            phase="finalize",
            resource_ids=ids,
            observed_statuses=statuses,
            rollback_errors=rollback_errors,
        )
        await _record_failure(
            engine,
            draft_id=draft_id,
            phase="finalize",
            budget_cents=budget_cents,
            error=failure,
            ids=ids,
            statuses=statuses,
            ads_manager_url=ads_manager_url,
        )
        rollback_state = "Rollback complete" if not rollback_errors else "Rollback incomplete"
        await notifier.notify_founder(
            f"⚠️ Meta activated draft #{draft_id}, but database finalization failed. "
            f"{rollback_state}; stored IDs and observed statuses were retained. "
            "Draft remains approved for retry."
        )
        return
    observed_state = activation.ad.get("effective_status", activation.ad.get("status", "ACTIVE"))
    await notifier.notify_founder(
        f"📣 *Meta ad active for draft #{draft_id}*\n"
        f"Open in Ads Manager: {ads_manager_url}\n"
        f"State: {observed_state} · Audience: {metadata['audience_profile_key']} · "
        f"Budget: €{budget_cents / 100:g}/day"
    )
    log.info("meta_pipeline.success", draft_id=draft_id, ad_id=ids["ad_id"])
