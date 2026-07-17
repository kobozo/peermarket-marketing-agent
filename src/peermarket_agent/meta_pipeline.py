"""Approved Meta draft to durable, activated publication."""

import json
from dataclasses import dataclass

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from peermarket_agent.autonomy.contracts import DecisionKind
from peermarket_agent.autonomy.replacements import ReplacementDraft
from peermarket_agent.autonomy.store import ClaimedAction
from peermarket_agent.campaign_urls import build_campaign_url
from peermarket_agent.config import Settings
from peermarket_agent.meta_ads import (
    MetaAdsDisabled,
    MetaAdsError,
    MetaConfig,
    activate_meta_ad,
    create_meta_ad_paused,
    get_meta_ad_statuses,
    pause_meta_ad,
)
from peermarket_agent.nano_banana import (
    ImageEditDisabled,
    ImageEditError,
    edit_image,
)
from peermarket_agent.publications import (
    MetaPublication,
    MetaReplacementHistoryError,
    begin_meta_terminal_replacement,
    get_meta_publication,
    record_meta_replacement_result,
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
_META_RECONCILIATION_ID_KEYS = {"campaign_id", "ad_set_id", "creative_id", "ad_id"}
_TERMINAL_META_STATUSES = {"ARCHIVED", "DELETED"}


@dataclass(frozen=True)
class TerminalReplacementResult:
    old_ids: dict[str, str]
    terminal_statuses: dict[str, dict[str, str]]
    current_ids: dict[str, str]
    state: str
    failure: dict | None


@dataclass(frozen=True)
class ReplacementPublication:
    draft_id: int
    source_draft_id: int
    locale: str
    external_ids: dict[str, str]
    ads_manager_url: str
    status: str = "PAUSED"


@dataclass(frozen=True)
class _PublishedReplacementAuthorization:
    draft_id: int
    attempt_id: str


class TerminalReplacementOperationalError(RuntimeError):
    """Sanitized post-transition replacement failure for operator surfaces."""


_REPLACEMENT_METADATA_KEYS = {
    "audience_profile_key",
    "headline",
    "description",
    "cta_type",
    "primary_text",
    "suggested_daily_budget_eur",
}
_REPLACEMENT_TEXT_METADATA_KEYS = _REPLACEMENT_METADATA_KEYS - {"suggested_daily_budget_eur"}


def _meta_config(settings: Settings) -> MetaConfig:
    return MetaConfig(
        app_id=settings.meta_app_id,
        app_secret=settings.meta_app_secret,
        system_user_token=settings.meta_system_user_token,
        ad_account_id=settings.meta_ad_account_id,
        page_id=settings.meta_page_id,
    )


async def _require_live_replacement_claim(
    engine: AsyncEngine, claim: ClaimedAction, draft: ReplacementDraft
) -> None:
    """Bind publication to the exact persisted decision, lease, and generated draft."""
    if claim.kind is not DecisionKind.REPLACE or claim.decision.kind is not DecisionKind.REPLACE:
        raise ValueError("autonomous replacement requires a replace claim")
    async with engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT a.status, a.campaign_id, d.decision_key, d.evidence, r.metadata "
                        "FROM autonomous_actions a JOIN autonomous_decisions d ON d.id=a.decision_id "
                        "JOIN drafts r ON r.id=:draft_id WHERE a.id=:action_id "
                        "AND a.decision_id=:decision_id AND a.lease_owner=:owner "
                        "AND a.lease_token=:token AND a.lease_expires_at>NOW()"
                    ),
                    {
                        "draft_id": draft.id,
                        "action_id": claim.id,
                        "decision_id": claim.decision_id,
                        "owner": claim.lease_owner,
                        "token": claim.lease_token,
                    },
                )
            )
            .mappings()
            .first()
        )
    if row is None or row["status"] not in {"leased", "executing"}:
        raise ValueError("autonomous replacement requires a live persisted worker claim")
    if (
        row["campaign_id"] != claim.campaign_id
        or row["decision_key"] != claim.decision.idempotency_key
        or row["evidence"] != claim.decision.evidence
    ):
        raise ValueError("persisted autonomous decision differs from frozen claim")
    metadata = row["metadata"] or {}
    expected_locales = {
        key: {
            "locale": item.locale,
            "primary_text": item.primary_text,
            "headline": item.headline,
            "description": item.description,
            "cta_label": item.cta_label,
            "audience_profile_key": item.audience_profile_key,
        }
        for key, item in draft.locales.items()
    }
    if (
        metadata.get("source_draft_id") != draft.source_draft_id
        or metadata.get("experiment_id") != draft.experiment_id
        or metadata.get("changed_dimension") != draft.changed_dimension
        or metadata.get("suggested_daily_budget_eur") != draft.daily_budget_eur
        or metadata.get("landing_page_url") != draft.landing_page_url
        or metadata.get("locales") != expected_locales
    ):
        raise ValueError("persisted replacement metadata differs from frozen draft")


async def publish_replacement_paused(
    *,
    engine: AsyncEngine,
    settings: Settings,
    claim: ClaimedAction,
    draft: ReplacementDraft,
    locale: str = "NL",
) -> ReplacementPublication:
    """Create only paused replacement resources; lifecycle ordering belongs to executor."""
    await _require_live_replacement_claim(engine, claim, draft)
    if set(draft.locales) != {"NL", "FR", "EN"} or locale not in draft.locales:
        raise ValueError("replacement must contain exact NL/FR/EN locales")
    creative = draft.locales[locale]
    screenshot = await screenshot_url(
        draft.landing_page_url, viewport_width=1080, viewport_height=1080
    )
    image = screenshot
    try:
        image = await edit_image(
            api_key=settings.gemini_api_key, image_bytes=screenshot, prompt=_BRAND_FRAME_PROMPT
        )
    except (ImageEditDisabled, ImageEditError):
        image = screenshot
    cta_type = {
        "Learn More": "LEARN_MORE",
        "Sign Up": "SIGN_UP",
        "Shop Now": "SHOP_NOW",
        "Get Started": "GET_STARTED",
    }[creative.cta_label]
    result = await create_meta_ad_paused(
        config=_meta_config(settings),
        name=f"PeerMarket autonomous draft #{draft.id} {locale}",
        primary_text=creative.primary_text,
        headline=creative.headline,
        description=creative.description,
        cta_type=cta_type,
        landing_page_url=draft.landing_page_url,
        image_bytes=image,
        audience_profile_key=creative.audience_profile_key,
        daily_budget_eur=draft.daily_budget_eur,
    )
    if result.status != "PAUSED":
        raise RuntimeError("Meta replacement creation did not return PAUSED")
    ids = {
        "campaign_id": result.campaign_id,
        "ad_set_id": result.ad_set_id,
        "creative_id": result.creative_id,
        "ad_id": result.ad_id,
    }
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO creatives_archive (asset_path, prompt, model, cost_cents, performance_summary) "
                "VALUES (:path, :prompt, 'autonomous-replacement', 0, CAST(:summary AS JSONB))"
            ),
            {
                "path": f"autonomous:draft-{draft.id}:{locale}",
                "prompt": _BRAND_FRAME_PROMPT,
                "summary": json.dumps({"source_draft_id": draft.source_draft_id, "meta_ids": ids}),
            },
        )
    return ReplacementPublication(
        draft_id=draft.id,
        source_draft_id=draft.source_draft_id,
        locale=locale,
        external_ids=ids,
        ads_manager_url=result.ads_manager_url,
    )


async def _refuse_terminal_replacement(
    notifier: SlackNotifier, draft_id: int, message: str
) -> None:
    try:
        await notifier.notify_founder(f"⚠️ Draft #{draft_id}: {message} No replacement was started.")
    except Exception:
        log.exception("meta_pipeline.replacement_refusal_notification_failed", draft_id=draft_id)
    raise ValueError(message)


async def replace_terminal_meta_draft(
    *,
    engine: AsyncEngine,
    draft_id: int,
    settings: Settings,
    notifier: SlackNotifier,
    expected_ids: dict[str, str],
) -> TerminalReplacementResult:
    """Explicitly replace one exact, entirely terminal Meta hierarchy."""
    valid_ids = set(expected_ids) == _META_RECONCILIATION_ID_KEYS and all(
        isinstance(value, str) and value and value == value.strip()
        for value in expected_ids.values()
    )
    if not valid_ids:
        await _refuse_terminal_replacement(
            notifier,
            draft_id,
            "refusing replacement: IDs must contain exact non-empty current Meta IDs",
        )
    async with engine.begin() as lock_connection:
        await lock_connection.execute(
            text(
                "SELECT pg_advisory_xact_lock("
                "hashtext('peermarket_meta_pipeline'), hashint8(:draft_id))"
            ),
            {"draft_id": draft_id},
        )
        draft = await _fetch_meta_draft(engine, draft_id)
        publication = await get_meta_publication(engine, draft_id)
        if draft is None or draft[0] not in {"approved", "published"}:
            await _refuse_terminal_replacement(
                notifier,
                draft_id,
                f"refusing replacement: draft #{draft_id} is not approved or published",
            )
        replacement_draft_status = draft[0]
        if publication is None or publication.external_ids != expected_ids:
            await _refuse_terminal_replacement(
                notifier,
                draft_id,
                "refusing replacement: supplied IDs are not the exact stored current IDs",
            )
        metadata = draft[1]
        if not settings.meta_auto_activate:
            await _refuse_terminal_replacement(
                notifier, draft_id, "refusing replacement: automatic Meta activation is disabled"
            )
        if (
            not _REPLACEMENT_METADATA_KEYS.issubset(metadata)
            or any(
                not isinstance(metadata[key], str) or not metadata[key].strip()
                for key in _REPLACEMENT_TEXT_METADATA_KEYS
            )
            or not isinstance(metadata["suggested_daily_budget_eur"], int)
            or isinstance(metadata["suggested_daily_budget_eur"], bool)
            or metadata["suggested_daily_budget_eur"] <= 0
        ):
            await _refuse_terminal_replacement(
                notifier,
                draft_id,
                "refusing replacement: draft metadata is structurally incomplete",
            )
        budget_cents = publication.approved_budget_cents
        if budget_cents is None or budget_cents <= 0:
            await _refuse_terminal_replacement(
                notifier,
                draft_id,
                "refusing replacement: approved budget is not frozen and positive",
            )
        if budget_cents % 100:
            await _refuse_terminal_replacement(
                notifier,
                draft_id,
                "refusing replacement: frozen budget must be an exact whole euro",
            )
        config = _meta_config(settings)
        if any(
            not value
            for value in (
                config.app_id,
                config.app_secret,
                config.system_user_token,
                config.ad_account_id,
                config.page_id,
            )
        ):
            await _refuse_terminal_replacement(
                notifier,
                draft_id,
                "refusing replacement: Meta connector configuration is incomplete",
            )
        try:
            statuses = await get_meta_ad_statuses(config, expected_ids)
        except (MetaAdsDisabled, MetaAdsError) as error:
            await _refuse_terminal_replacement(
                notifier,
                draft_id,
                f"refusing replacement: unable to read every Meta status: {error}",
            )
        except Exception:
            await _refuse_terminal_replacement(
                notifier, draft_id, "refusing replacement: unable to read every Meta status"
            )
        terminal = set(statuses) == {"campaign", "ad_set", "ad"} and all(
            set(resource_status) >= {"status", "effective_status"}
            and resource_status["status"] in _TERMINAL_META_STATUSES
            and resource_status["effective_status"] in _TERMINAL_META_STATUSES
            for resource_status in statuses.values()
        )
        if not terminal:
            await _refuse_terminal_replacement(
                notifier,
                draft_id,
                "refusing replacement: stored Meta hierarchy is not entirely terminal "
                f"(ARCHIVED/DELETED): {json.dumps(statuses, sort_keys=True)}",
            )
        try:
            screenshot_bytes = await screenshot_url(
                _LANDING_PAGE, viewport_width=1080, viewport_height=1080
            )
            prepared_image = screenshot_bytes
            try:
                prepared_image = await edit_image(
                    api_key=settings.gemini_api_key,
                    image_bytes=screenshot_bytes,
                    prompt=_BRAND_FRAME_PROMPT,
                )
            except (ImageEditDisabled, ImageEditError):
                prepared_image = screenshot_bytes
        except Exception:
            await _refuse_terminal_replacement(
                notifier,
                draft_id,
                "refusing replacement: unable to prepare replacement image",
            )

        attempt_id = await begin_meta_terminal_replacement(engine, draft_id, expected_ids, statuses)
        authorization = (
            _PublishedReplacementAuthorization(draft_id=draft_id, attempt_id=attempt_id)
            if replacement_draft_status == "published"
            else None
        )
        operational_failure: dict | None = None
        result: TerminalReplacementResult | None = None
        try:
            try:
                await _process_approved_meta_draft(
                    engine=engine,
                    draft_id=draft_id,
                    settings=settings,
                    notifier=notifier,
                    prepared_image=prepared_image,
                    _replacement_authorization=authorization,
                )
            except Exception:
                operational_failure = {
                    "phase": "unexpected",
                    "message": "unexpected replacement pipeline failure",
                }
            current = await get_meta_publication(engine, draft_id)
            if current is None:
                operational_failure = {
                    "phase": "unexpected",
                    "message": "replacement publication disappeared",
                }
            elif (
                operational_failure is None
                and current.state == "creating"
                and not current.external_ids
            ):
                operational_failure = {
                    "phase": "unexpected",
                    "message": "replacement pipeline produced no current resources",
                }
            elif operational_failure is None and (current.state == "failed" or current.failure):
                operational_failure = current.failure or {
                    "phase": "pipeline",
                    "message": "replacement pipeline reported failure",
                }
            if operational_failure is not None:
                await upsert_meta_publication(
                    engine,
                    MetaPublication(
                        draft_id=draft_id,
                        state="failed",
                        failure=operational_failure,
                        approved_budget_cents=budget_cents,
                    ),
                )
                current = await get_meta_publication(engine, draft_id)
            assert current is not None
            result = TerminalReplacementResult(
                old_ids=expected_ids,
                terminal_statuses=statuses,
                current_ids=current.external_ids,
                state=current.state or "unknown",
                failure=current.failure,
            )
            try:
                await notifier.notify_founder(
                    f"Meta terminal replacement for draft #{draft_id}: archived hierarchy "
                    f"{json.dumps(expected_ids, sort_keys=True)}; current replacement "
                    f"{json.dumps(current.external_ids, sort_keys=True)}; state={result.state}; "
                    f"failure={json.dumps(result.failure, sort_keys=True) if result.failure else 'none'}."
                )
            except Exception:
                log.exception(
                    "meta_pipeline.replacement_result_notification_failed", draft_id=draft_id
                )
        finally:
            current = await get_meta_publication(engine, draft_id)
            final_state = (
                "failed" if operational_failure else (current.state if current else "failed")
            )
            final_failure = operational_failure or (
                current.failure if current else {"phase": "unexpected"}
            )
            try:
                await record_meta_replacement_result(
                    engine,
                    draft_id,
                    attempt_id,
                    state=final_state or "failed",
                    failure=final_failure,
                )
            except MetaReplacementHistoryError:
                raise TerminalReplacementOperationalError(
                    "terminal replacement history finalization failed; "
                    "publication state requires operator inspection"
                ) from None
        if operational_failure:
            phase = operational_failure.get("phase", "operational")
            current_ids = result.current_ids if result is not None else {}
            raise TerminalReplacementOperationalError(
                f"terminal replacement failed during {phase}; archived IDs "
                f"{json.dumps(expected_ids, sort_keys=True)}; current IDs "
                f"{json.dumps(current_ids, sort_keys=True)}; attempt state failed; "
                "inspect stored replacement history"
            )
        assert result is not None
        return result


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
    engine: AsyncEngine,
    draft_id: int,
    statuses: dict[str, dict[str, str]],
    *,
    replacement_authorization: _PublishedReplacementAuthorization | None = None,
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
        if replacement_authorization is None:
            draft_result = await connection.execute(
                text(
                    "UPDATE drafts SET status = 'published' "
                    "WHERE id = :draft_id AND status = 'approved'"
                ),
                {"draft_id": draft_id},
            )
        else:
            draft_result = await connection.execute(
                text(
                    "UPDATE drafts SET status = status WHERE id = :draft_id "
                    "AND status = 'published' AND EXISTS (SELECT 1 FROM publications p, "
                    "jsonb_array_elements(COALESCE(p.replacement_history, '[]'::JSONB)) item "
                    "WHERE p.draft_id = :draft_id "
                    "AND item->>'attempt_id' = :attempt_id "
                    "AND item->>'finished_at' IS NULL)"
                ),
                {
                    "draft_id": draft_id,
                    "attempt_id": replacement_authorization.attempt_id,
                },
            )
        if draft_result.rowcount != 1:
            expected = "published replacement attempt" if replacement_authorization else "approved"
            raise RuntimeError(f"draft was not {expected} during publication finalization")


async def process_approved_meta_draft(
    *,
    engine: AsyncEngine,
    draft_id: int,
    settings: Settings,
    notifier: SlackNotifier,
    reconciliation_ids: dict[str, str] | None = None,
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
        if reconciliation_ids is not None:
            valid_mapping = set(reconciliation_ids) == _META_RECONCILIATION_ID_KEYS and all(
                isinstance(value, str) and bool(value.strip()) and value == value.strip()
                for value in reconciliation_ids.values()
            )
            if not valid_mapping:
                raise ValueError(
                    "reconciliation IDs must contain exactly campaign_id, ad_set_id, "
                    "creative_id, and ad_id with non-empty stripped string values"
                )
            draft = await _fetch_meta_draft(engine, draft_id)
            if draft is None:
                raise ValueError(
                    f"refusing reconciliation: draft #{draft_id} does not exist "
                    "or is not a Meta ad draft"
                )
            draft_status, _ = draft
            if draft_status not in {"approved", "published"}:
                raise ValueError(
                    f"refusing reconciliation: draft #{draft_id} has status "
                    f"{draft_status!r}, expected 'approved'"
                )
            publication = await get_meta_publication(engine, draft_id)
            stored_ids = publication.external_ids if publication is not None else {}
            for key, supplied_value in reconciliation_ids.items():
                stored_value = stored_ids.get(key)
                if stored_value is not None and stored_value != supplied_value:
                    raise ValueError(
                        f"refusing reconciliation: supplied {key} {supplied_value!r} "
                        f"conflicts with stored value {stored_value!r}"
                    )
            if draft_status == "published" and not (reconciliation_ids.keys() <= stored_ids.keys()):
                raise ValueError(
                    f"refusing reconciliation: draft #{draft_id} is already published "
                    "but its stored Meta IDs are incomplete"
                )
            if not reconciliation_ids.keys() <= stored_ids.keys():
                await upsert_meta_publication(
                    engine,
                    MetaPublication(
                        draft_id=draft_id,
                        state="created",
                        external_ids=reconciliation_ids,
                    ),
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
    prepared_image: bytes | None = None,
    _replacement_authorization: _PublishedReplacementAuthorization | None = None,
) -> None:
    """End-to-end pipeline. Best-effort. Never raises connector failures."""
    log.info("meta_pipeline.start", draft_id=draft_id)
    draft = await _fetch_meta_draft(engine, draft_id)
    if draft is None:
        log.warning("meta_pipeline.draft_missing_or_wrong_type", draft_id=draft_id)
        return
    draft_status, metadata = draft
    publication = await get_meta_publication(engine, draft_id)
    authorized_published_replacement = (
        draft_status == "published"
        and _replacement_authorization is not None
        and _replacement_authorization.draft_id == draft_id
        and publication is not None
        and any(
            attempt.get("attempt_id") == _replacement_authorization.attempt_id
            and attempt.get("finished_at") is None
            for attempt in publication.replacement_history
        )
    )
    if draft_status == "published" and not authorized_published_replacement:
        log.info("meta_pipeline.already_published", draft_id=draft_id)
        return
    if not metadata:
        await notifier.notify_founder(
            f"⚠️ Couldn't push draft #{draft_id} to Meta — it was created before "
            "we added structured metadata. Regenerate it and re-approve."
        )
        return
    if draft_status != "approved" and not authorized_published_replacement:
        log.warning("meta_pipeline.draft_not_approved", draft_id=draft_id, status=draft_status)
        return
    draft_status_label = "Published" if authorized_published_replacement else "Approved"
    retained_draft_status = draft_status_label.lower()
    if not settings.meta_auto_activate:
        log.warning("meta_pipeline.auto_activation_disabled", draft_id=draft_id)
        await notifier.notify_founder(
            f"⚠️ Refusing to push draft #{draft_id}: automatic Meta activation is disabled. "
            "Set META_AUTO_ACTIVATE=true through the deployment workflow to enable it."
        )
        return

    metadata_budget_cents = int(metadata["suggested_daily_budget_eur"]) * 100
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
        if prepared_image is not None:
            image_bytes = prepared_image
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
                    f"⚠️ {draft_status_label} draft #{draft_id} but couldn't screenshot "
                    f"peermarket.eu: {e}. Draft remains {retained_draft_status}; retry after "
                    "fixing screenshot capture."
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
                    f"⚠️ {draft_status_label} draft #{draft_id}, screenshot OK, but image-edit "
                    f"failed: {e}. "
                    "Falling back to raw screenshot, continuing with Meta push."
                )

        meta_config = _meta_config(settings)
        try:
            result = await create_meta_ad_paused(
                config=meta_config,
                name=f"PeerMarket draft #{draft_id}",
                primary_text=metadata["primary_text"],
                headline=metadata["headline"],
                description=metadata["description"],
                cta_type=metadata["cta_type"],
                landing_page_url=(
                    _LANDING_PAGE
                    if authorized_published_replacement
                    else build_campaign_url(_LANDING_PAGE, draft_id)
                ),
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
                f"⚠️ {draft_status_label} draft #{draft_id}, but Meta connector isn't "
                f"configured: {e}. Set the META_* secrets + redeploy; draft remains "
                f"{retained_draft_status}."
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
                f"⚠️ {draft_status_label} draft #{draft_id}, but Meta creation failed: {e}. "
                f"Draft remains {retained_draft_status}; no automatic duplicate retry was "
                "attempted."
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
        page_id=settings.meta_page_id,
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
            f"{rollback_state}; stored IDs retained for reconciliation. Draft remains "
            f"{retained_draft_status}."
        )
        return

    statuses = {
        "campaign": activation.campaign,
        "ad_set": activation.ad_set,
        "ad": activation.ad,
    }
    try:
        await _mark_published(
            engine,
            draft_id,
            statuses,
            replacement_authorization=_replacement_authorization,
        )
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
        if authorized_published_replacement:
            retained_state_guidance = (
                "Draft remains published. Retained IDs and replacement history require "
                "operator inspection; the replacement command must not be retried blindly."
            )
        else:
            retained_state_guidance = "Draft remains approved for retry."
        await notifier.notify_founder(
            f"⚠️ Meta activated draft #{draft_id}, but database finalization failed. "
            f"{rollback_state}; stored IDs and observed statuses were retained. "
            f"{retained_state_guidance}"
        )
        return
    observed_state = activation.ad.get("effective_status", activation.ad.get("status", "ACTIVE"))
    try:
        await notifier.notify_founder(
            f"📣 *Meta ad active for draft #{draft_id}*\n"
            f"Open in Ads Manager: {ads_manager_url}\n"
            f"State: {observed_state} · Audience: {metadata['audience_profile_key']} · "
            f"Budget: €{budget_cents / 100:g}/day"
        )
    except Exception:
        log.exception("meta_pipeline.success_notification_failed", draft_id=draft_id)
    log.info("meta_pipeline.success", draft_id=draft_id, ad_id=ids["ad_id"])
