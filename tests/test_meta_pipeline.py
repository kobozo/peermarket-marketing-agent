"""Meta pipeline tests — approval → screenshot → brand-frame → paused Meta ad."""

import asyncio
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
from peermarket_agent.meta_pipeline import (
    TerminalReplacementOperationalError,
    _mark_published,
    _process_approved_meta_draft,
    process_approved_meta_draft,
    replace_terminal_meta_draft,
)
from peermarket_agent.nano_banana import ImageEditDisabled
from peermarket_agent.publications import (
    MetaPublication,
    MetaReplacementHistoryError,
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


def _patch_replacement_preparation(monkeypatch):
    monkeypatch.setattr(
        "peermarket_agent.meta_pipeline.screenshot_url", AsyncMock(return_value=b"raw")
    )
    monkeypatch.setattr(
        "peermarket_agent.meta_pipeline.edit_image", AsyncMock(return_value=b"prepared")
    )


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


@pytest.mark.parametrize(
    "statuses",
    [
        {
            "campaign": {"status": "PAUSED", "effective_status": "PAUSED"},
            "ad_set": {"status": "ARCHIVED", "effective_status": "ARCHIVED"},
            "ad": {"status": "ARCHIVED", "effective_status": "ARCHIVED"},
        },
        {
            "campaign": {"status": "ARCHIVED", "effective_status": "ARCHIVED"},
            "ad_set": {"status": "DELETED", "effective_status": "DELETED"},
        },
        {
            "campaign": {"status": "ARCHIVED", "effective_status": "UNKNOWN"},
            "ad_set": {"status": "ARCHIVED", "effective_status": "ARCHIVED"},
            "ad": {"status": "ARCHIVED", "effective_status": "ARCHIVED"},
        },
    ],
)
async def test_terminal_replacement_refuses_nonterminal_missing_or_unknown_without_write_or_create(
    monkeypatch, engine_with_meta_draft, statuses
):
    engine, draft_id, _ = engine_with_meta_draft
    ids = {"campaign_id": "old-c", "ad_set_id": "old-s", "creative_id": "old-cr", "ad_id": "old-a"}
    await upsert_meta_publication(
        engine,
        MetaPublication(
            draft_id=draft_id, state="failed", external_ids=ids, approved_budget_cents=1000
        ),
    )
    monkeypatch.setattr(
        "peermarket_agent.meta_pipeline.get_meta_ad_statuses", AsyncMock(return_value=statuses)
    )
    create = AsyncMock()
    notifier = AsyncMock()
    monkeypatch.setattr("peermarket_agent.meta_pipeline.create_meta_ad_paused", create)

    with pytest.raises(ValueError, match="not entirely terminal"):
        await replace_terminal_meta_draft(
            engine=engine,
            draft_id=draft_id,
            settings=_make_settings(),
            notifier=notifier,
            expected_ids=ids,
        )

    stored = await get_meta_publication(engine, draft_id)
    assert stored.external_ids == ids
    assert stored.replacement_history == []
    create.assert_not_awaited()
    notifier.notify_founder.assert_awaited_once()


async def test_terminal_replacement_archives_exact_ids_then_runs_normal_pipeline_once(
    monkeypatch, engine_with_meta_draft
):
    engine, draft_id, _ = engine_with_meta_draft
    ids = {"campaign_id": "old-c", "ad_set_id": "old-s", "creative_id": "old-cr", "ad_id": "old-a"}
    await upsert_meta_publication(
        engine,
        MetaPublication(
            draft_id=draft_id, state="failed", external_ids=ids, approved_budget_cents=1000
        ),
    )
    statuses = {
        name: {"status": "ARCHIVED", "effective_status": "ARCHIVED"}
        for name in ("campaign", "ad_set", "ad")
    }
    monkeypatch.setattr(
        "peermarket_agent.meta_pipeline.get_meta_ad_statuses", AsyncMock(return_value=statuses)
    )
    _patch_replacement_preparation(monkeypatch)

    async def successful_replacement(**kwargs):
        await upsert_meta_publication(
            engine,
            MetaPublication(
                draft_id=draft_id,
                state="active",
                external_ids={
                    "campaign_id": "new-c",
                    "ad_set_id": "new-s",
                    "creative_id": "new-cr",
                    "ad_id": "new-a",
                },
            ),
        )

    normal = AsyncMock(side_effect=successful_replacement)
    monkeypatch.setattr("peermarket_agent.meta_pipeline._process_approved_meta_draft", normal)

    result = await replace_terminal_meta_draft(
        engine=engine,
        draft_id=draft_id,
        settings=_make_settings(),
        notifier=AsyncMock(),
        expected_ids=ids,
    )

    normal.assert_awaited_once()
    stored = await get_meta_publication(engine, draft_id)
    assert stored.external_ids["campaign_id"] == "new-c"
    assert stored.approved_budget_cents == 1000
    assert stored.replacement_history[0]["old_ids"] == ids
    assert result.old_ids == ids


async def test_terminal_replacement_partial_creation_is_current_and_historical(
    monkeypatch, engine_with_meta_draft
):
    engine, draft_id, _ = engine_with_meta_draft
    ids = {"campaign_id": "old-c", "ad_set_id": "old-s", "creative_id": "old-cr", "ad_id": "old-a"}
    await upsert_meta_publication(
        engine,
        MetaPublication(
            draft_id=draft_id, state="failed", external_ids=ids, approved_budget_cents=1000
        ),
    )
    statuses = {
        name: {"status": "DELETED", "effective_status": "DELETED"}
        for name in ("campaign", "ad_set", "ad")
    }
    monkeypatch.setattr(
        "peermarket_agent.meta_pipeline.get_meta_ad_statuses", AsyncMock(return_value=statuses)
    )
    _patch_replacement_preparation(monkeypatch)

    async def partial(**kwargs):
        await upsert_meta_publication(
            engine,
            MetaPublication(
                draft_id=draft_id,
                state="failed",
                external_ids={"campaign_id": "new-c"},
                failure={"phase": "create_ad_set"},
            ),
        )

    monkeypatch.setattr(
        "peermarket_agent.meta_pipeline._process_approved_meta_draft",
        AsyncMock(side_effect=partial),
    )
    notifier = AsyncMock()
    with pytest.raises(Exception, match="replacement failed"):
        await replace_terminal_meta_draft(
            engine=engine,
            draft_id=draft_id,
            settings=_make_settings(),
            notifier=notifier,
            expected_ids=ids,
        )

    stored = await get_meta_publication(engine, draft_id)
    assert stored.replacement_history[0]["old_ids"] == ids
    assert len(stored.replacement_history) == 1
    assert stored.replacement_history[0]["replacement_ids"] == {"campaign_id": "new-c"}
    assert "old-c" in notifier.notify_founder.await_args.args[0]
    assert "new-c" in notifier.notify_founder.await_args.args[0]


async def test_concurrent_terminal_replacements_create_only_once(
    monkeypatch, engine_with_meta_draft
):
    engine, draft_id, _ = engine_with_meta_draft
    ids = {"campaign_id": "old-c", "ad_set_id": "old-s", "creative_id": "old-cr", "ad_id": "old-a"}
    await upsert_meta_publication(
        engine,
        MetaPublication(
            draft_id=draft_id, state="failed", external_ids=ids, approved_budget_cents=1000
        ),
    )
    statuses = {
        name: {"status": "ARCHIVED", "effective_status": "ARCHIVED"}
        for name in ("campaign", "ad_set", "ad")
    }
    status_read = AsyncMock(return_value=statuses)

    async def successful_replacement(**kwargs):
        await upsert_meta_publication(
            engine,
            MetaPublication(
                draft_id=draft_id,
                state="active",
                external_ids={
                    "campaign_id": "new-c",
                    "ad_set_id": "new-s",
                    "creative_id": "new-cr",
                    "ad_id": "new-a",
                },
            ),
        )

    normal = AsyncMock(side_effect=successful_replacement)
    monkeypatch.setattr("peermarket_agent.meta_pipeline.get_meta_ad_statuses", status_read)
    _patch_replacement_preparation(monkeypatch)
    monkeypatch.setattr("peermarket_agent.meta_pipeline._process_approved_meta_draft", normal)

    outcomes = await asyncio.gather(
        *[
            replace_terminal_meta_draft(
                engine=engine,
                draft_id=draft_id,
                settings=_make_settings(),
                notifier=AsyncMock(),
                expected_ids=ids,
            )
            for _ in range(2)
        ],
        return_exceptions=True,
    )

    assert sum(not isinstance(value, Exception) for value in outcomes) == 1
    assert normal.await_count == 1
    assert status_read.await_count == 1


@pytest.mark.parametrize(
    ("settings", "budget_cents", "metadata_patch", "message"),
    [
        (_make_settings(meta_auto_activate=False), 1000, {}, "automatic Meta activation"),
        (_make_settings(), 1050, {}, "whole euro"),
        (_make_settings(), 1000, {"headline": None}, "metadata"),
        (_make_settings(), 1000, {"headline": {}}, "metadata"),
        (_make_settings(meta_page_id=""), 1000, {}, "connector configuration"),
    ],
)
async def test_terminal_replacement_refuses_invalid_local_prerequisite_without_mutation(
    monkeypatch, engine_with_meta_draft, settings, budget_cents, metadata_patch, message
):
    engine, draft_id, _ = engine_with_meta_draft
    ids = {"campaign_id": "old-c", "ad_set_id": "old-s", "creative_id": "old-cr", "ad_id": "old-a"}
    await upsert_meta_publication(
        engine,
        MetaPublication(
            draft_id=draft_id, state="failed", external_ids=ids, approved_budget_cents=budget_cents
        ),
    )
    if metadata_patch:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE drafts SET metadata = metadata || CAST(:patch AS JSONB) WHERE id = :id"
                ),
                {"id": draft_id, "patch": __import__("json").dumps(metadata_patch)},
            )
    status_read = AsyncMock()
    create = AsyncMock()
    notifier = AsyncMock()
    monkeypatch.setattr("peermarket_agent.meta_pipeline.get_meta_ad_statuses", status_read)
    monkeypatch.setattr("peermarket_agent.meta_pipeline.create_meta_ad_paused", create)

    with pytest.raises(ValueError, match=message):
        await replace_terminal_meta_draft(
            engine=engine,
            draft_id=draft_id,
            settings=settings,
            notifier=notifier,
            expected_ids=ids,
        )

    stored = await get_meta_publication(engine, draft_id)
    assert stored.external_ids == ids
    assert stored.replacement_history == []
    status_read.assert_not_awaited()
    create.assert_not_awaited()
    notifier.notify_founder.assert_awaited_once()
    assert "refusing" in notifier.notify_founder.await_args.args[0]


async def test_terminal_replacement_refuses_screenshot_failure_without_mutation(
    monkeypatch, engine_with_meta_draft
):
    engine, draft_id, _ = engine_with_meta_draft
    ids = {"campaign_id": "old-c", "ad_set_id": "old-s", "creative_id": "old-cr", "ad_id": "old-a"}
    await upsert_meta_publication(
        engine,
        MetaPublication(
            draft_id=draft_id, state="failed", external_ids=ids, approved_budget_cents=1000
        ),
    )
    statuses = {
        name: {"status": "ARCHIVED", "effective_status": "ARCHIVED"}
        for name in ("campaign", "ad_set", "ad")
    }
    monkeypatch.setattr(
        "peermarket_agent.meta_pipeline.get_meta_ad_statuses", AsyncMock(return_value=statuses)
    )
    monkeypatch.setattr(
        "peermarket_agent.meta_pipeline.screenshot_url",
        AsyncMock(side_effect=ScreenshotError("browser down")),
    )
    create = AsyncMock()
    notifier = AsyncMock()
    monkeypatch.setattr("peermarket_agent.meta_pipeline.create_meta_ad_paused", create)

    with pytest.raises(ValueError, match="prepare replacement image"):
        await replace_terminal_meta_draft(
            engine=engine,
            draft_id=draft_id,
            settings=_make_settings(),
            notifier=notifier,
            expected_ids=ids,
        )
    stored = await get_meta_publication(engine, draft_id)
    assert stored.external_ids == ids
    assert stored.replacement_history == []
    create.assert_not_awaited()
    notifier.notify_founder.assert_awaited_once()


async def test_terminal_replacement_finalizes_single_history_entry_on_unexpected_connector_exception(
    monkeypatch, engine_with_meta_draft
):
    engine, draft_id, _ = engine_with_meta_draft
    ids = {"campaign_id": "old-c", "ad_set_id": "old-s", "creative_id": "old-cr", "ad_id": "old-a"}
    await upsert_meta_publication(
        engine,
        MetaPublication(
            draft_id=draft_id, state="failed", external_ids=ids, approved_budget_cents=1000
        ),
    )
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE drafts SET status = 'published' WHERE id = :id"), {"id": draft_id}
        )
    statuses = {
        name: {"status": "ARCHIVED", "effective_status": "ARCHIVED"}
        for name in ("campaign", "ad_set", "ad")
    }
    monkeypatch.setattr(
        "peermarket_agent.meta_pipeline.get_meta_ad_statuses", AsyncMock(return_value=statuses)
    )
    _patch_replacement_preparation(monkeypatch)
    notifier = AsyncMock()
    async def partial_then_crash(**kwargs):
        await upsert_meta_publication(
            engine,
            MetaPublication(
                draft_id=draft_id,
                state="creating",
                external_ids={"campaign_id": "new-c"},
            ),
        )
        raise RuntimeError("token secret raw")

    monkeypatch.setattr("peermarket_agent.meta_pipeline.create_meta_ad_paused", partial_then_crash)

    with pytest.raises(Exception, match="replacement failed"):
        await replace_terminal_meta_draft(
            engine=engine,
            draft_id=draft_id,
            settings=_make_settings(),
            notifier=notifier,
            expected_ids=ids,
        )

    stored = await get_meta_publication(engine, draft_id)
    assert len(stored.replacement_history) == 1
    attempt = stored.replacement_history[0]
    assert attempt["attempt_id"]
    assert attempt["started_at"]
    assert attempt["finished_at"]
    assert attempt["old_ids"] == ids
    assert attempt["replacement_ids"] == {"campaign_id": "new-c"}
    assert attempt["state"] == "failed"
    assert attempt["failure"]["phase"] == "unexpected"
    assert "token secret raw" not in str(attempt)
    async with engine.connect() as connection:
        assert (
            await connection.execute(
                text("SELECT status FROM drafts WHERE id = :id"), {"id": draft_id}
            )
        ).scalar_one() == "published"


async def test_terminal_replacement_success_survives_real_lifecycle_notifier_failure(
    monkeypatch, engine_with_meta_draft
):
    engine, draft_id, _ = engine_with_meta_draft
    old_ids = {
        "campaign_id": "old-c",
        "ad_set_id": "old-s",
        "creative_id": "old-cr",
        "ad_id": "old-a",
    }
    await upsert_meta_publication(
        engine,
        MetaPublication(
            draft_id=draft_id, state="failed", external_ids=old_ids, approved_budget_cents=1000
        ),
    )
    terminal = {
        name: {"status": "ARCHIVED", "effective_status": "ARCHIVED"}
        for name in ("campaign", "ad_set", "ad")
    }
    monkeypatch.setattr(
        "peermarket_agent.meta_pipeline.get_meta_ad_statuses", AsyncMock(return_value=terminal)
    )
    _patch_replacement_preparation(monkeypatch)
    monkeypatch.setattr(
        "peermarket_agent.meta_pipeline.create_meta_ad_paused",
        AsyncMock(
            return_value=MetaAdResult(
                ad_id="new-a",
                ad_set_id="new-s",
                campaign_id="new-c",
                creative_id="new-cr",
                ads_manager_url="https://example.test/new-a",
                status="PAUSED",
            )
        ),
    )
    monkeypatch.setattr(
        "peermarket_agent.meta_pipeline.activate_meta_ad",
        AsyncMock(
            return_value=MetaActivationResult(
                campaign={"status": "ACTIVE", "effective_status": "ACTIVE"},
                ad_set={"status": "ACTIVE", "effective_status": "ACTIVE"},
                ad={"status": "ACTIVE", "effective_status": "PENDING_REVIEW"},
            )
        ),
    )
    notifier = AsyncMock()
    notifier.notify_founder = AsyncMock(side_effect=RuntimeError("slack unavailable"))

    result = await replace_terminal_meta_draft(
        engine=engine,
        draft_id=draft_id,
        settings=_make_settings(),
        notifier=notifier,
        expected_ids=old_ids,
    )

    stored = await get_meta_publication(engine, draft_id)
    assert result.state == "active"
    assert result.current_ids["ad_id"] == "new-a"
    assert stored.state == "active"
    assert stored.failure is None
    assert stored.replacement_history[0]["state"] == "active"
    assert stored.replacement_history[0]["failure"] is None
    async with engine.connect() as connection:
        draft_status = (
            await connection.execute(
                text("SELECT status FROM drafts WHERE id = :id"), {"id": draft_id}
            )
        ).scalar_one()
    assert draft_status == "published"


async def test_published_terminal_replacement_runs_authorized_lifecycle_without_status_regression(
    monkeypatch, engine_with_meta_draft
):
    engine, draft_id, _ = engine_with_meta_draft
    old_ids = {
        "campaign_id": "old-c",
        "ad_set_id": "old-s",
        "creative_id": "old-cr",
        "ad_id": "old-a",
    }
    new_ids = {
        "campaign_id": "new-c",
        "ad_set_id": "new-s",
        "creative_id": "new-cr",
        "ad_id": "new-a",
    }
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE drafts SET status = 'published' WHERE id = :id"), {"id": draft_id}
        )
    await upsert_meta_publication(
        engine,
        MetaPublication(
            draft_id=draft_id,
            state="active",
            external_ids=old_ids,
            approved_budget_cents=800,
        ),
    )
    terminal = {
        name: {"status": "ARCHIVED", "effective_status": "ARCHIVED"}
        for name in ("campaign", "ad_set", "ad")
    }
    monkeypatch.setattr(
        "peermarket_agent.meta_pipeline.get_meta_ad_statuses", AsyncMock(return_value=terminal)
    )
    _patch_replacement_preparation(monkeypatch)
    monkeypatch.setattr(
        "peermarket_agent.meta_pipeline.create_meta_ad_paused",
        AsyncMock(
            return_value=MetaAdResult(
                ad_id=new_ids["ad_id"],
                ad_set_id=new_ids["ad_set_id"],
                campaign_id=new_ids["campaign_id"],
                creative_id=new_ids["creative_id"],
                ads_manager_url="https://example.test/new-a",
                status="PAUSED",
            )
        ),
    )
    monkeypatch.setattr(
        "peermarket_agent.meta_pipeline.activate_meta_ad",
        AsyncMock(
            return_value=MetaActivationResult(
                campaign={"status": "ACTIVE", "effective_status": "ACTIVE"},
                ad_set={"status": "ACTIVE", "effective_status": "ACTIVE"},
                ad={"status": "ACTIVE", "effective_status": "PENDING_REVIEW"},
            )
        ),
    )
    observed_statuses = []
    real_fetch = __import__(
        "peermarket_agent.meta_pipeline", fromlist=["_fetch_meta_draft"]
    )._fetch_meta_draft

    async def observing_fetch(*args, **kwargs):
        draft = await real_fetch(*args, **kwargs)
        if draft is not None:
            observed_statuses.append(draft[0])
        return draft

    monkeypatch.setattr("peermarket_agent.meta_pipeline._fetch_meta_draft", observing_fetch)

    result = await replace_terminal_meta_draft(
        engine=engine,
        draft_id=draft_id,
        settings=_make_settings(),
        notifier=AsyncMock(),
        expected_ids=old_ids,
    )

    stored = await get_meta_publication(engine, draft_id)
    async with engine.connect() as connection:
        draft_status = (
            await connection.execute(
                text("SELECT status FROM drafts WHERE id = :id"), {"id": draft_id}
            )
        ).scalar_one()
    assert result.old_ids == old_ids
    assert result.current_ids == new_ids
    assert result.state == "active"
    assert stored.external_ids == new_ids
    assert stored.replacement_history[-1]["old_ids"] == old_ids
    assert stored.replacement_history[-1]["replacement_ids"] == new_ids
    assert stored.replacement_history[-1]["state"] == "active"
    assert draft_status == "published"
    assert observed_statuses
    assert "approved" not in observed_statuses


async def test_private_lifecycle_without_replacement_authorization_is_noop_for_published(
    monkeypatch, engine_with_meta_draft
):
    engine, draft_id, _ = engine_with_meta_draft
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE drafts SET status = 'published' WHERE id = :id"), {"id": draft_id}
        )
    create = AsyncMock()
    monkeypatch.setattr("peermarket_agent.meta_pipeline.create_meta_ad_paused", create)

    await _process_approved_meta_draft(
        engine=engine,
        draft_id=draft_id,
        settings=_make_settings(),
        notifier=AsyncMock(),
    )

    create.assert_not_awaited()
    assert await get_meta_publication(engine, draft_id) is None


@pytest.mark.parametrize("failure_phase", ["create", "activate", "finalize"])
async def test_published_replacement_failure_notifications_preserve_published_wording(
    monkeypatch, engine_with_meta_draft, failure_phase
):
    engine, draft_id, _ = engine_with_meta_draft
    old_ids = {
        "campaign_id": "old-c",
        "ad_set_id": "old-s",
        "creative_id": "old-cr",
        "ad_id": "old-a",
    }
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE drafts SET status = 'published' WHERE id = :id"), {"id": draft_id}
        )
    await upsert_meta_publication(
        engine,
        MetaPublication(
            draft_id=draft_id,
            state="active",
            external_ids=old_ids,
            approved_budget_cents=800,
        ),
    )
    terminal = {
        name: {"status": "ARCHIVED", "effective_status": "ARCHIVED"}
        for name in ("campaign", "ad_set", "ad")
    }
    monkeypatch.setattr(
        "peermarket_agent.meta_pipeline.get_meta_ad_statuses", AsyncMock(return_value=terminal)
    )
    _patch_replacement_preparation(monkeypatch)
    created = MetaAdResult(
        ad_id="new-a",
        ad_set_id="new-s",
        campaign_id="new-c",
        creative_id="new-cr",
        ads_manager_url="https://example.test/new-a",
        status="PAUSED",
    )
    activation = MetaActivationResult(
        campaign={"status": "ACTIVE", "effective_status": "ACTIVE"},
        ad_set={"status": "ACTIVE", "effective_status": "ACTIVE"},
        ad={"status": "ACTIVE", "effective_status": "PENDING_REVIEW"},
    )
    monkeypatch.setattr(
        "peermarket_agent.meta_pipeline.create_meta_ad_paused",
        AsyncMock(
            side_effect=MetaAdsError("sanitized create failure", phase="create")
            if failure_phase == "create"
            else None,
            return_value=created,
        ),
    )
    monkeypatch.setattr(
        "peermarket_agent.meta_pipeline.activate_meta_ad",
        AsyncMock(
            side_effect=MetaAdsError("sanitized activation failure", phase="activate")
            if failure_phase == "activate"
            else None,
            return_value=activation,
        ),
    )
    if failure_phase == "finalize":
        monkeypatch.setattr(
            "peermarket_agent.meta_pipeline._mark_published",
            AsyncMock(side_effect=RuntimeError("database race")),
        )
        monkeypatch.setattr("peermarket_agent.meta_pipeline.pause_meta_ad", AsyncMock(return_value={}))
    notifier = AsyncMock()

    with pytest.raises(TerminalReplacementOperationalError):
        await replace_terminal_meta_draft(
            engine=engine,
            draft_id=draft_id,
            settings=_make_settings(),
            notifier=notifier,
            expected_ids=old_ids,
        )

    notifications = "\n".join(call.args[0] for call in notifier.notify_founder.await_args_list)
    assert "published" in notifications.lower()
    assert "approved" not in notifications.lower()
    if failure_phase == "finalize":
        assert "operator inspection" in notifications.lower()
        assert "must not be retried blindly" in notifications.lower()
        assert "for retry" not in notifications.lower()


async def test_terminal_replacement_sanitizes_history_finalizer_validation_failure(
    monkeypatch, engine_with_meta_draft
):
    engine, draft_id, _ = engine_with_meta_draft
    old_ids = {
        "campaign_id": "old-c",
        "ad_set_id": "old-s",
        "creative_id": "old-cr",
        "ad_id": "old-a",
    }
    await upsert_meta_publication(
        engine,
        MetaPublication(
            draft_id=draft_id, state="failed", external_ids=old_ids, approved_budget_cents=1000
        ),
    )
    terminal = {
        name: {"status": "ARCHIVED", "effective_status": "ARCHIVED"}
        for name in ("campaign", "ad_set", "ad")
    }
    monkeypatch.setattr(
        "peermarket_agent.meta_pipeline.get_meta_ad_statuses", AsyncMock(return_value=terminal)
    )
    _patch_replacement_preparation(monkeypatch)

    async def successful_replacement(**kwargs):
        await upsert_meta_publication(
            engine,
            MetaPublication(
                draft_id=draft_id,
                state="active",
                external_ids={
                    "campaign_id": "new-c",
                    "ad_set_id": "new-s",
                    "creative_id": "new-cr",
                    "ad_id": "new-a",
                },
            ),
        )

    monkeypatch.setattr(
        "peermarket_agent.meta_pipeline._process_approved_meta_draft",
        AsyncMock(side_effect=successful_replacement),
    )
    monkeypatch.setattr(
        "peermarket_agent.meta_pipeline.record_meta_replacement_result",
        AsyncMock(side_effect=MetaReplacementHistoryError("mismatch raw detail")),
    )

    with pytest.raises(Exception, match="history finalization failed") as error:
        await replace_terminal_meta_draft(
            engine=engine,
            draft_id=draft_id,
            settings=_make_settings(),
            notifier=AsyncMock(),
            expected_ids=old_ids,
        )

    assert "mismatch raw detail" not in str(error.value)


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
    assert "Draft remains approved" in notifier.notify_founder.await_args.args[0]


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
    assert "Draft remains approved" in notifier.notify_founder.await_args.args[0]
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
    assert "draft remains approved" in msg


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
