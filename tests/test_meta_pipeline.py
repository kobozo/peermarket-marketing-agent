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
from peermarket_agent.meta_ads import MetaAdResult, MetaAdsDisabled
from peermarket_agent.meta_pipeline import process_approved_meta_draft
from peermarket_agent.nano_banana import ImageEditDisabled
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
        meta_page_id="61592144690879",
        resend_api_key="re",
        backblaze_b2_key_id="kid",
        backblaze_b2_app_key="akey",
        backblaze_b2_bucket="bkt",
        backblaze_b2_endpoint="endpoint",
    )
    base.update(overrides)
    return Settings(**base)


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
    monkeypatch.setattr("peermarket_agent.meta_pipeline.create_paused_ad", create_mock)

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
    # create_paused_ad got the framed bytes + structured metadata
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

    notifier.notify_founder.assert_awaited_once()
    msg = notifier.notify_founder.await_args.args[0]
    assert f"draft #{draft_id}" in msg
    assert "https://business.facebook.com/adsmanager" in msg
    assert "PAUSED" in msg


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
    monkeypatch.setattr("peermarket_agent.meta_pipeline.create_paused_ad", create_mock)

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
    assert "Created paused Meta ad" in notifier.notify_founder.await_args.args[0]


async def test_screenshot_failure_dms_founder_and_aborts(monkeypatch, engine_with_meta_draft):
    engine, draft_id, _ = engine_with_meta_draft

    monkeypatch.setattr(
        "peermarket_agent.meta_pipeline.screenshot_url",
        AsyncMock(side_effect=ScreenshotError("timeout after 30000ms")),
    )
    edit_mock = AsyncMock()
    monkeypatch.setattr("peermarket_agent.meta_pipeline.edit_image", edit_mock)
    create_mock = AsyncMock()
    monkeypatch.setattr("peermarket_agent.meta_pipeline.create_paused_ad", create_mock)

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
        "peermarket_agent.meta_pipeline.create_paused_ad",
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
