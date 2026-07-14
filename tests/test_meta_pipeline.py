"""Meta pipeline tests — approval → screenshot → brand-frame → paused Meta ad."""

import os
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from peermarket_agent.config import Settings
from peermarket_agent.db.migrations import run_migrations
from peermarket_agent.db.seed import seed
from peermarket_agent.drafts import Draft, persist_draft
from peermarket_agent.meta_ads import (
    MetaActivationResult,
    MetaAdResult,
    MetaAdsDisabled,
    MetaAdsError,
)
from peermarket_agent.meta_pipeline import _mark_published, process_approved_meta_draft
from peermarket_agent.nano_banana import ImageEditDisabled
from peermarket_agent.publications import (
    MetaPublication,
    get_meta_publication,
    upsert_meta_publication,
)
from peermarket_agent.screenshots import ScreenshotError


def _make_settings(**overrides) -> Settings:
    base = dict(
        anthropic_api_key="sk-ant-test",
        slack_bot_token="xoxb-test",
        slack_app_token="xapp-test",
        slack_signing_secret="sig",
        slack_founder_user_id="U0FOUNDER",
        agent_db_url="postgresql+asyncpg://x:y@localhost/z",
        peermarket_prod_db_readonly_url="postgresql+asyncpg://r:o@host/peer",
        github_app_id=1,
        github_app_private_key="-----BEGIN RSA PRIVATE KEY-----\nx\n-----END RSA PRIVATE KEY-----",
        github_app_installation_id=1,
        recraft_api_key="rk",
        gemini_api_key="gk",
        meta_app_id="ma",
        meta_app_secret="msec",
        meta_system_user_token="mtok",
        meta_ad_account_id="act_999",
        meta_auto_activate=True,
        meta_page_id="61592144690879",
        resend_api_key="re",
        backblaze_b2_key_id="kid",
        backblaze_b2_app_key="akey",
        backblaze_b2_bucket="bkt",
        backblaze_b2_endpoint="endpoint",
    )
    base.update(overrides)
    return Settings(**base)


async def test_pipeline_refuses_automatic_activation_when_disabled(
    monkeypatch, engine_with_meta_draft
):
    engine, draft_id, _ = engine_with_meta_draft
    screenshot_mock = AsyncMock()
    create_mock = AsyncMock()
    activate_mock = AsyncMock()
    monkeypatch.setattr("peermarket_agent.meta_pipeline.screenshot_url", screenshot_mock)
    monkeypatch.setattr("peermarket_agent.meta_pipeline.create_meta_ad_paused", create_mock)
    monkeypatch.setattr("peermarket_agent.meta_pipeline.activate_meta_ad", activate_mock)
    notifier = AsyncMock()
    notifier.notify_founder = AsyncMock(return_value=True)

    await process_approved_meta_draft(
        engine=engine,
        draft_id=draft_id,
        settings=_make_settings(meta_auto_activate=False),
        notifier=notifier,
    )

    screenshot_mock.assert_not_awaited()
    create_mock.assert_not_awaited()
    activate_mock.assert_not_awaited()
    assert await get_meta_publication(engine, draft_id) is None
    notifier.notify_founder.assert_awaited_once()
    assert "automatic Meta activation is disabled" in notifier.notify_founder.await_args.args[0]


@pytest.fixture
async def engine_with_meta_draft():
    url = os.environ["AGENT_DB_URL"]
    eng = create_async_engine(url, future=True)
    async with eng.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await run_migrations(eng)
    await seed(eng)
    metadata = {
        "audience_profile_key": "declutterers",
        "headline": "Verkoop veilig",
        "description": "Met geverifieerde",
        "cta_label": "Learn More",
        "cta_type": "LEARN_MORE",
        "suggested_daily_budget_eur": 10,
        "primary_text": (
            "PeerMarket is de Belgische marktplaats waar elke verkoper "
            "zijn identiteit verifieert. Verkoop veilig, koop met vertrouwen. "
            "Plaats vandaag je eerste item gratis."
        ),
    }
    draft_id = await persist_draft(
        eng,
        Draft(
            action_type_name="meta_ad_creative",
            channel="meta",
            language="NL",
            copy="x",
            asset_path=None,
            generation_cost_cents=2,
            brand_score=88,
            visual_truthfulness_pass=True,
            metadata=metadata,
        ),
    )
    async with eng.begin() as conn:
        await conn.execute(
            text("UPDATE drafts SET status = 'approved' WHERE id = :id"),
            {"id": draft_id},
        )
    yield eng, draft_id, metadata
    await eng.dispose()


async def test_full_happy_path(monkeypatch, engine_with_meta_draft):
    engine, draft_id, metadata = engine_with_meta_draft

    screenshot_mock = AsyncMock(return_value=b"raw-png-bytes")
    monkeypatch.setattr("peermarket_agent.meta_pipeline.screenshot_url", screenshot_mock)
    edit_mock = AsyncMock(return_value=b"framed-png-bytes")
    monkeypatch.setattr("peermarket_agent.meta_pipeline.edit_image", edit_mock)
    create_mock = AsyncMock(
        return_value=MetaAdResult(
            ad_id="ad123",
            ad_set_id="as1",
            campaign_id="c1",
            creative_id="cr1",
            ads_manager_url="https://business.facebook.com/adsmanager/manage/ads?act=999&selected_ad_ids=ad123",
            status="PAUSED",
        )
    )
    monkeypatch.setattr("peermarket_agent.meta_pipeline.create_meta_ad_paused", create_mock)
    activation = MetaActivationResult(
        campaign={"status": "ACTIVE", "effective_status": "ACTIVE"},
        ad_set={"status": "ACTIVE", "effective_status": "ACTIVE"},
        ad={"status": "ACTIVE", "effective_status": "PENDING_REVIEW"},
    )

    async def activate_after_ids_are_durable(config, ids):
        stored = await get_meta_publication(engine, draft_id)
        assert stored is not None
        assert stored.external_ids == {
            "campaign_id": "c1",
            "ad_set_id": "as1",
            "creative_id": "cr1",
            "ad_id": "ad123",
        }
        assert stored.approved_budget_cents == 1000
        return activation

    activate_mock = AsyncMock(side_effect=activate_after_ids_are_durable)
    monkeypatch.setattr("peermarket_agent.meta_pipeline.activate_meta_ad", activate_mock)

    notifier = AsyncMock()
    notifier.notify_founder = AsyncMock(return_value=True)
    settings = _make_settings()

    await process_approved_meta_draft(
        engine=engine,
        draft_id=draft_id,
        settings=settings,
        notifier=notifier,
    )

    screenshot_mock.assert_awaited_once()
    edit_mock.assert_awaited_once()
    # edit_image got raw screenshot bytes
    assert edit_mock.await_args.kwargs["image_bytes"] == b"raw-png-bytes"
    # create_meta_ad_paused got the framed bytes + structured metadata
    create_kwargs = create_mock.await_args.kwargs
    assert create_kwargs["image_bytes"] == b"framed-png-bytes"
    assert create_kwargs["primary_text"] == metadata["primary_text"]
    assert create_kwargs["headline"] == metadata["headline"]
    assert create_kwargs["description"] == metadata["description"]
    assert create_kwargs["cta_type"] == "LEARN_MORE"
    assert create_kwargs["audience_profile_key"] == "declutterers"
    assert create_kwargs["daily_budget_eur"] == 10
    assert create_kwargs["config"].page_id == "61592144690879"
    assert f"draft-{draft_id}" in create_kwargs["landing_page_url"]
    assert "utm_source=meta" in create_kwargs["landing_page_url"]

    activate_mock.assert_awaited_once()
    notifier.notify_founder.assert_awaited_once()
    msg = notifier.notify_founder.await_args.args[0]
    assert f"draft #{draft_id}" in msg
    assert "https://business.facebook.com/adsmanager" in msg
    assert "PENDING_REVIEW" in msg
    assert "activate when ready" not in msg
    stored = await get_meta_publication(engine, draft_id)
    assert stored is not None
    assert stored.state == "active"
    assert stored.external_statuses["ad"]["effective_status"] == "PENDING_REVIEW"
    async with engine.connect() as conn:
        status = (
            await conn.execute(text("SELECT status FROM drafts WHERE id = :id"), {"id": draft_id})
        ).scalar_one()
    assert status == "published"


async def test_existing_publication_reconciles_without_creating_resources(
    monkeypatch, engine_with_meta_draft
):
    engine, draft_id, _ = engine_with_meta_draft
    ids = {
        "campaign_id": "existing-campaign",
        "ad_set_id": "existing-adset",
        "creative_id": "existing-creative",
        "ad_id": "existing-ad",
    }
    await upsert_meta_publication(
        engine,
        MetaPublication(
            draft_id=draft_id,
            state="created",
            external_ids=ids,
            approved_budget_cents=1000,
            ads_manager_url="https://example.test/ads-manager",
        ),
    )
    screenshot_mock = AsyncMock()
    create_mock = AsyncMock()
    monkeypatch.setattr("peermarket_agent.meta_pipeline.screenshot_url", screenshot_mock)
    monkeypatch.setattr("peermarket_agent.meta_pipeline.create_meta_ad_paused", create_mock)
    activate_mock = AsyncMock(
        return_value=MetaActivationResult(
            campaign={"status": "ACTIVE", "effective_status": "ACTIVE"},
            ad_set={"status": "ACTIVE", "effective_status": "ACTIVE"},
            ad={"status": "ACTIVE", "effective_status": "IN_PROCESS"},
        )
    )
    monkeypatch.setattr(
        "peermarket_agent.meta_pipeline.activate_meta_ad", activate_mock, raising=False
    )
    notifier = AsyncMock()

    await process_approved_meta_draft(
        engine=engine, draft_id=draft_id, settings=_make_settings(), notifier=notifier
    )

    screenshot_mock.assert_not_awaited()
    create_mock.assert_not_awaited()
    assert activate_mock.await_args.args[1] == ids


async def test_retry_uses_frozen_approved_budget(monkeypatch, engine_with_meta_draft):
    engine, draft_id, _ = engine_with_meta_draft
    ids = {"campaign_id": "c1", "ad_set_id": "as1", "ad_id": "ad1"}
    await upsert_meta_publication(
        engine,
        MetaPublication(
            draft_id=draft_id,
            state="created",
            external_ids=ids,
            approved_budget_cents=1000,
        ),
    )
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE drafts SET metadata = jsonb_set(metadata, "
                "'{suggested_daily_budget_eur}', '99'::jsonb) WHERE id = :id"
            ),
            {"id": draft_id},
        )
    monkeypatch.setattr(
        "peermarket_agent.meta_pipeline.activate_meta_ad",
        AsyncMock(
            return_value=MetaActivationResult(
                campaign={"status": "ACTIVE", "effective_status": "ACTIVE"},
                ad_set={"status": "ACTIVE", "effective_status": "ACTIVE"},
                ad={"status": "ACTIVE", "effective_status": "ACTIVE"},
            )
        ),
    )
    notifier = AsyncMock()

    await process_approved_meta_draft(
        engine=engine, draft_id=draft_id, settings=_make_settings(), notifier=notifier
    )

    assert "Budget: €10/day" in notifier.notify_founder.await_args.args[0]
    assert "€99" not in notifier.notify_founder.await_args.args[0]


async def test_legacy_publication_freezes_budget_before_activation(
    monkeypatch, engine_with_meta_draft
):
    engine, draft_id, _ = engine_with_meta_draft
    ids = {"campaign_id": "c1", "ad_set_id": "as1", "ad_id": "ad1"}
    await upsert_meta_publication(
        engine, MetaPublication(draft_id=draft_id, state="created", external_ids=ids)
    )

    async def activate_after_budget_is_frozen(config, resource_ids):
        stored = await get_meta_publication(engine, draft_id)
        assert stored is not None
        assert stored.approved_budget_cents == 1000
        return MetaActivationResult(
            campaign={"status": "ACTIVE", "effective_status": "ACTIVE"},
            ad_set={"status": "ACTIVE", "effective_status": "ACTIVE"},
            ad={"status": "ACTIVE", "effective_status": "ACTIVE"},
        )

    monkeypatch.setattr(
        "peermarket_agent.meta_pipeline.activate_meta_ad",
        AsyncMock(side_effect=activate_after_budget_is_frozen),
    )

    await process_approved_meta_draft(
        engine=engine, draft_id=draft_id, settings=_make_settings(), notifier=AsyncMock()
    )


async def test_activation_failure_retains_ids_diagnostics_and_approved_status(
    monkeypatch, engine_with_meta_draft
):
    engine, draft_id, _ = engine_with_meta_draft
    monkeypatch.setattr(
        "peermarket_agent.meta_pipeline.screenshot_url", AsyncMock(return_value=b"raw")
    )
    monkeypatch.setattr(
        "peermarket_agent.meta_pipeline.edit_image", AsyncMock(return_value=b"framed")
    )
    monkeypatch.setattr(
        "peermarket_agent.meta_pipeline.create_meta_ad_paused",
        AsyncMock(
            return_value=MetaAdResult(
                ad_id="ad1",
                ad_set_id="as1",
                campaign_id="c1",
                creative_id="cr1",
                ads_manager_url="https://example.test/ad1",
                status="PAUSED",
            )
        ),
    )
    error = MetaAdsError(
        "activation failed",
        phase="activate_ad_set",
        resource_ids={"campaign_id": "c1", "ad_set_id": "as1", "ad_id": "ad1"},
        observed_statuses={"campaign": {"status": "ACTIVE"}},
        rollback_errors={},
    )
    monkeypatch.setattr(
        "peermarket_agent.meta_pipeline.activate_meta_ad",
        AsyncMock(side_effect=error),
        raising=False,
    )
    notifier = AsyncMock()

    await process_approved_meta_draft(
        engine=engine, draft_id=draft_id, settings=_make_settings(), notifier=notifier
    )

    stored = await get_meta_publication(engine, draft_id)
    assert stored is not None
    assert stored.external_ids["ad_id"] == "ad1"
    assert stored.failure == {
        "phase": "activate_ad_set",
        "rollback_complete": True,
        "rollback_errors": {},
    }
    async with engine.connect() as conn:
        status = (
            await conn.execute(text("SELECT status FROM drafts WHERE id = :id"), {"id": draft_id})
        ).scalar_one()
    assert status == "approved"
    notifier.notify_founder.assert_awaited_once()
    assert "Rollback complete" in notifier.notify_founder.await_args.args[0]


async def test_published_retry_is_noop(monkeypatch, engine_with_meta_draft):
    engine, draft_id, _ = engine_with_meta_draft
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE drafts SET status = 'published' WHERE id = :id"), {"id": draft_id}
        )
    screenshot_mock = AsyncMock()
    activate_mock = AsyncMock()
    monkeypatch.setattr("peermarket_agent.meta_pipeline.screenshot_url", screenshot_mock)
    monkeypatch.setattr(
        "peermarket_agent.meta_pipeline.activate_meta_ad", activate_mock, raising=False
    )
    notifier = AsyncMock()

    await process_approved_meta_draft(
        engine=engine, draft_id=draft_id, settings=_make_settings(), notifier=notifier
    )

    screenshot_mock.assert_not_awaited()
    activate_mock.assert_not_awaited()
    notifier.notify_founder.assert_not_awaited()


async def test_finalization_failure_retains_reconciliation_state(
    monkeypatch, engine_with_meta_draft
):
    engine, draft_id, _ = engine_with_meta_draft
    ids = {"campaign_id": "c1", "ad_set_id": "as1", "ad_id": "ad1"}
    await upsert_meta_publication(
        engine,
        MetaPublication(
            draft_id=draft_id,
            state="created",
            external_ids=ids,
            approved_budget_cents=1000,
        ),
    )
    monkeypatch.setattr(
        "peermarket_agent.meta_pipeline.activate_meta_ad",
        AsyncMock(
            return_value=MetaActivationResult(
                campaign={"status": "ACTIVE", "effective_status": "ACTIVE"},
                ad_set={"status": "ACTIVE", "effective_status": "ACTIVE"},
                ad={"status": "ACTIVE", "effective_status": "ACTIVE"},
            )
        ),
    )
    monkeypatch.setattr(
        "peermarket_agent.meta_pipeline._mark_published",
        AsyncMock(side_effect=RuntimeError("database unavailable")),
    )
    pause_mock = AsyncMock(return_value={"ad": "pause rejected"})
    monkeypatch.setattr("peermarket_agent.meta_pipeline.pause_meta_ad", pause_mock, raising=False)
    notifier = AsyncMock()

    await process_approved_meta_draft(
        engine=engine, draft_id=draft_id, settings=_make_settings(), notifier=notifier
    )

    stored = await get_meta_publication(engine, draft_id)
    assert stored is not None
    assert stored.external_ids == ids
    assert stored.failure["phase"] == "finalize"
    assert stored.failure["rollback_complete"] is False
    assert stored.failure["rollback_errors"] == {"ad": "pause rejected"}
    pause_mock.assert_awaited_once()
    assert pause_mock.await_args.args[1] == ids
    notifier.notify_founder.assert_awaited_once()
    assert "Rollback incomplete" in notifier.notify_founder.await_args.args[0]


async def test_mark_published_rejects_non_approved_draft(engine_with_meta_draft):
    engine, draft_id, _ = engine_with_meta_draft
    await upsert_meta_publication(
        engine,
        MetaPublication(draft_id=draft_id, state="created", external_ids={"ad_id": "ad1"}),
    )
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE drafts SET status = 'rejected' WHERE id = :id"), {"id": draft_id}
        )

    with pytest.raises(RuntimeError, match="approved"):
        await _mark_published(engine, draft_id, {"ad": {"status": "ACTIVE"}})

    stored = await get_meta_publication(engine, draft_id)
    assert stored is not None
    assert stored.state == "created"


async def test_partial_creation_failure_persists_ids_and_retry_refuses_duplicate(
    monkeypatch, engine_with_meta_draft
):
    engine, draft_id, _ = engine_with_meta_draft
    monkeypatch.setattr(
        "peermarket_agent.meta_pipeline.screenshot_url", AsyncMock(return_value=b"raw")
    )
    monkeypatch.setattr(
        "peermarket_agent.meta_pipeline.edit_image", AsyncMock(return_value=b"framed")
    )
    create_mock = AsyncMock(
        side_effect=MetaAdsError(
            "creation failed",
            phase="create_ad",
            resource_ids={
                "campaign_id": "c1",
                "ad_set_id": "as1",
                "creative_id": "cr1",
            },
            rollback_errors={"ad_set": "pause rejected"},
        )
    )
    monkeypatch.setattr("peermarket_agent.meta_pipeline.create_meta_ad_paused", create_mock)
    notifier = AsyncMock()

    await process_approved_meta_draft(
        engine=engine, draft_id=draft_id, settings=_make_settings(), notifier=notifier
    )
    stored = await get_meta_publication(engine, draft_id)
    assert stored is not None
    assert stored.external_ids == {
        "campaign_id": "c1",
        "ad_set_id": "as1",
        "creative_id": "cr1",
    }

    create_mock.reset_mock()
    await process_approved_meta_draft(
        engine=engine, draft_id=draft_id, settings=_make_settings(), notifier=notifier
    )
    create_mock.assert_not_awaited()
    stored = await get_meta_publication(engine, draft_id)
    assert stored is not None
    assert stored.failure["rollback_complete"] is False
    assert stored.failure["rollback_errors"] == {"ad_set": "pause rejected"}
    assert "Rollback complete: no" in notifier.notify_founder.await_args.args[0]


async def test_nano_banana_disabled_falls_back_to_raw_screenshot(
    monkeypatch, engine_with_meta_draft
):
    engine, draft_id, _ = engine_with_meta_draft

    monkeypatch.setattr(
        "peermarket_agent.meta_pipeline.screenshot_url",
        AsyncMock(return_value=b"raw-png"),
    )
    monkeypatch.setattr(
        "peermarket_agent.meta_pipeline.edit_image",
        AsyncMock(side_effect=ImageEditDisabled("no gemini key")),
    )
    create_mock = AsyncMock(
        return_value=MetaAdResult(
            ad_id="ad1",
            ad_set_id="as1",
            campaign_id="c1",
            creative_id="cr1",
            ads_manager_url="https://example/url",
            status="PAUSED",
        )
    )
    monkeypatch.setattr("peermarket_agent.meta_pipeline.create_meta_ad_paused", create_mock)
    monkeypatch.setattr(
        "peermarket_agent.meta_pipeline.activate_meta_ad",
        AsyncMock(
            return_value=MetaActivationResult(
                campaign={"status": "ACTIVE", "effective_status": "ACTIVE"},
                ad_set={"status": "ACTIVE", "effective_status": "ACTIVE"},
                ad={"status": "ACTIVE", "effective_status": "ACTIVE"},
            )
        ),
    )

    notifier = AsyncMock()
    notifier.notify_founder = AsyncMock(return_value=True)

    await process_approved_meta_draft(
        engine=engine,
        draft_id=draft_id,
        settings=_make_settings(gemini_api_key=""),
        notifier=notifier,
    )

    create_mock.assert_awaited_once()
    # Fell back to the raw screenshot bytes
    assert create_mock.await_args.kwargs["image_bytes"] == b"raw-png"
    # Founder still got the success DM (only one — no warning, since disabled is silent)
    assert notifier.notify_founder.await_count == 1
    assert "Meta ad active" in notifier.notify_founder.await_args.args[0]


async def test_screenshot_failure_dms_founder_and_aborts(monkeypatch, engine_with_meta_draft):
    engine, draft_id, _ = engine_with_meta_draft

    monkeypatch.setattr(
        "peermarket_agent.meta_pipeline.screenshot_url",
        AsyncMock(side_effect=ScreenshotError("timeout after 30000ms")),
    )
    edit_mock = AsyncMock()
    monkeypatch.setattr("peermarket_agent.meta_pipeline.edit_image", edit_mock)
    create_mock = AsyncMock()
    monkeypatch.setattr("peermarket_agent.meta_pipeline.create_meta_ad_paused", create_mock)

    notifier = AsyncMock()
    notifier.notify_founder = AsyncMock(return_value=True)

    await process_approved_meta_draft(
        engine=engine,
        draft_id=draft_id,
        settings=_make_settings(),
        notifier=notifier,
    )

    edit_mock.assert_not_awaited()
    create_mock.assert_not_awaited()
    notifier.notify_founder.assert_awaited_once()
    msg = notifier.notify_founder.await_args.args[0]
    assert "couldn't screenshot" in msg
    assert "timeout" in msg


async def test_meta_disabled_dms_founder(monkeypatch, engine_with_meta_draft):
    engine, draft_id, _ = engine_with_meta_draft

    monkeypatch.setattr(
        "peermarket_agent.meta_pipeline.screenshot_url",
        AsyncMock(return_value=b"raw"),
    )
    monkeypatch.setattr(
        "peermarket_agent.meta_pipeline.edit_image",
        AsyncMock(return_value=b"framed"),
    )
    monkeypatch.setattr(
        "peermarket_agent.meta_pipeline.create_meta_ad_paused",
        AsyncMock(side_effect=MetaAdsDisabled("missing credentials")),
    )

    notifier = AsyncMock()
    notifier.notify_founder = AsyncMock(return_value=True)

    await process_approved_meta_draft(
        engine=engine,
        draft_id=draft_id,
        settings=_make_settings(meta_app_id=""),
        notifier=notifier,
    )

    notifier.notify_founder.assert_awaited_once()
    msg = notifier.notify_founder.await_args.args[0]
    assert "Meta connector isn't configured" in msg
    assert "META_*" in msg


async def test_non_meta_draft_is_skipped_silently(monkeypatch):
    """If the draft is not a meta_ad_creative draft, pipeline returns silently."""
    url = os.environ["AGENT_DB_URL"]
    eng = create_async_engine(url, future=True)
    try:
        async with eng.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
        await run_migrations(eng)
        await seed(eng)
        # tiktok draft, not a meta_ad_creative
        draft_id = await persist_draft(
            eng,
            Draft(
                action_type_name="tiktok_post_organic",
                channel="tiktok",
                language="NL",
                copy="x",
                asset_path=None,
                generation_cost_cents=1,
                brand_score=88,
                visual_truthfulness_pass=True,
            ),
        )

        screenshot_mock = AsyncMock()
        monkeypatch.setattr("peermarket_agent.meta_pipeline.screenshot_url", screenshot_mock)
        notifier = AsyncMock()
        notifier.notify_founder = AsyncMock(return_value=True)

        await process_approved_meta_draft(
            engine=eng,
            draft_id=draft_id,
            settings=_make_settings(),
            notifier=notifier,
        )

        screenshot_mock.assert_not_awaited()
        notifier.notify_founder.assert_not_awaited()
    finally:
        await eng.dispose()


async def test_empty_metadata_legacy_draft_dms_founder(monkeypatch):
    """A meta_ad_creative draft with empty metadata (created before this feature)
    should not crash — should DM founder to regenerate."""
    url = os.environ["AGENT_DB_URL"]
    eng = create_async_engine(url, future=True)
    try:
        async with eng.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
        await run_migrations(eng)
        await seed(eng)
        # legacy draft — explicit empty metadata
        draft_id = await persist_draft(
            eng,
            Draft(
                action_type_name="meta_ad_creative",
                channel="meta",
                language="NL",
                copy="legacy copy",
                asset_path=None,
                generation_cost_cents=2,
                brand_score=88,
                visual_truthfulness_pass=True,
                metadata={},
            ),
        )

        screenshot_mock = AsyncMock()
        monkeypatch.setattr("peermarket_agent.meta_pipeline.screenshot_url", screenshot_mock)
        notifier = AsyncMock()
        notifier.notify_founder = AsyncMock(return_value=True)

        await process_approved_meta_draft(
            engine=eng,
            draft_id=draft_id,
            settings=_make_settings(),
            notifier=notifier,
        )

        # No screenshot was attempted
        screenshot_mock.assert_not_awaited()
        notifier.notify_founder.assert_awaited_once()
        msg = notifier.notify_founder.await_args.args[0]
        assert "before we added structured metadata" in msg
        assert "Regenerate" in msg
    finally:
        await eng.dispose()
