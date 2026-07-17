"""Drafts module tests — DB persistence + lookup helpers."""

import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from peermarket_agent.db.migrations import run_migrations
from peermarket_agent.db.seed import seed
from peermarket_agent.drafts import (
    Draft,
    VideoAsset,
    claim_video_asset,
    count_drafts_for_action,
    get_video_asset_by_slack_file,
    persist_draft,
    persist_video_asset,
)
from peermarket_agent.video_workflow import get_source_assets


@pytest.fixture
async def engine():
    url = os.environ["AGENT_DB_URL"]
    eng = create_async_engine(url, future=True)
    async with eng.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await run_migrations(eng)
    await seed(eng)
    yield eng
    await eng.dispose()


async def test_persist_draft_writes_row(engine):
    draft = Draft(
        action_type_name="tiktok_post_organic",
        channel="tiktok",
        language="NL",
        copy="Marktplaats moe? Verkoop veilig op PeerMarket.",
        asset_path=None,
        generation_cost_cents=1,
        brand_score=92,
        visual_truthfulness_pass=True,
    )
    draft_id = await persist_draft(engine, draft)
    assert isinstance(draft_id, int)

    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT channel, language, copy, brand_score, status FROM drafts WHERE id = :id"
                ),
                {"id": draft_id},
            )
        ).fetchone()
    assert row[0] == "tiktok"
    assert row[1] == "NL"
    assert row[2] == "Marktplaats moe? Verkoop veilig op PeerMarket."
    assert row[3] == 92
    assert row[4] == "queued"


async def test_persist_draft_resolves_action_type_id(engine):
    draft = Draft(
        action_type_name="email_re_engagement",
        channel="email",
        language="NL",
        copy="Je hebt nog niets verkocht.",
        asset_path=None,
        generation_cost_cents=2,
        brand_score=88,
        visual_truthfulness_pass=True,
    )
    await persist_draft(engine, draft)

    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT at.name FROM drafts d "
                    "JOIN action_types at ON at.id = d.action_type_id "
                    "ORDER BY d.id DESC LIMIT 1"
                )
            )
        ).fetchone()
    assert row[0] == "email_re_engagement"


async def test_persist_draft_unknown_action_type_raises(engine):
    draft = Draft(
        action_type_name="not_a_real_action",
        channel="x",
        language="NL",
        copy="x",
        asset_path=None,
        generation_cost_cents=0,
        brand_score=0,
        visual_truthfulness_pass=True,
    )
    with pytest.raises(ValueError, match="unknown action_type"):
        await persist_draft(engine, draft)


async def test_count_drafts_for_action(engine):
    # 3 of one type, 1 of another
    for _ in range(3):
        await persist_draft(
            engine,
            Draft(
                action_type_name="tiktok_post_organic",
                channel="tiktok",
                language="NL",
                copy="x",
                asset_path=None,
                generation_cost_cents=0,
                brand_score=80,
                visual_truthfulness_pass=True,
            ),
        )
    await persist_draft(
        engine,
        Draft(
            action_type_name="email_re_engagement",
            channel="email",
            language="NL",
            copy="x",
            asset_path=None,
            generation_cost_cents=0,
            brand_score=80,
            visual_truthfulness_pass=True,
        ),
    )
    assert await count_drafts_for_action(engine, "tiktok_post_organic") == 3
    assert await count_drafts_for_action(engine, "email_re_engagement") == 1


async def test_persist_draft_stores_metadata(engine):
    metadata = {
        "audience_profile_key": "declutterers",
        "headline": "Verkoop veilig",
        "cta_type": "LEARN_MORE",
        "suggested_daily_budget_eur": 10,
    }
    draft_id = await persist_draft(
        engine,
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
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT metadata FROM drafts WHERE id = :id"),
                {"id": draft_id},
            )
        ).fetchone()
    assert row[0] == metadata


async def test_persist_video_asset_stores_and_retrieves_source_video(engine):
    draft_id = await persist_draft(
        engine,
        Draft(
            action_type_name="tiktok_post_organic",
            channel="tiktok",
            language="NL",
            copy="x",
            asset_path=None,
            generation_cost_cents=0,
            brand_score=80,
            visual_truthfulness_pass=True,
            metadata={"slack_channel_id": "C123", "slack_ts": "1234.5678"},
        ),
    )
    asset = VideoAsset(
        draft_id=draft_id,
        slack_file_id="F123",
        thread_ts="1234.5678",
        path="/tmp/source.mp4",
        role="source",
        mime_type="video/mp4",
        size_bytes=1234,
        duration_seconds=12.5,
        width=1080,
        height=1920,
        status="downloaded",
        review={"speaker": "human"},
    )

    asset_id = await persist_video_asset(engine, asset)
    stored = await get_video_asset_by_slack_file(engine, draft_id, "F123")

    assert isinstance(asset_id, int)
    assert stored == asset


async def test_persist_video_asset_returns_existing_id_for_duplicate_slack_file(engine):
    draft_id = await persist_draft(
        engine,
        Draft(
            action_type_name="tiktok_post_organic",
            channel="tiktok",
            language="NL",
            copy="x",
            asset_path=None,
            generation_cost_cents=0,
            brand_score=80,
            visual_truthfulness_pass=True,
            metadata={"slack_channel_id": "C123", "slack_ts": "1234.5678"},
        ),
    )
    asset = VideoAsset(
        draft_id=draft_id,
        slack_file_id="F123",
        thread_ts="1234.5678",
        path="/tmp/source.mp4",
        role="source",
        mime_type="video/mp4",
        size_bytes=1234,
        duration_seconds=None,
        width=None,
        height=None,
        status="queued",
        review={},
    )

    first_id = await persist_video_asset(engine, asset)
    second_id = await persist_video_asset(engine, asset)

    assert second_id == first_id
    async with engine.connect() as conn:
        count = await conn.scalar(
            text("SELECT count(*) FROM video_assets WHERE draft_id = :id"), {"id": draft_id}
        )
    assert count == 1


async def test_claim_video_asset_returns_existing_asset_without_a_second_claim(engine):
    draft_id = await persist_draft(
        engine,
        Draft("tiktok_post_organic", "tiktok", "NL", "x", None, 0, 80, True),
    )
    asset = VideoAsset(
        draft_id,
        "F123",
        "1234.5678",
        "/tmp/source.mp4",
        "source",
        "video/mp4",
        1234,
        None,
        None,
        None,
        "accepted",
        {},
    )

    first, first_claimed = await claim_video_asset(engine, asset)
    second, second_claimed = await claim_video_asset(engine, asset)

    assert first == asset
    assert first_claimed is True
    assert second == asset
    assert second_claimed is False


async def test_get_source_assets_excludes_rejected_and_failed_sources(engine):
    draft_id = await persist_draft(
        engine,
        Draft("tiktok_post_organic", "tiktok", "NL", "x", None, 0, 80, True),
    )
    for file_id, status in (
        ("Fgood", "reviewed"),
        ("Frejected", "rejected"),
        ("Ffailed", "failed"),
    ):
        await persist_video_asset(
            engine,
            VideoAsset(
                draft_id,
                file_id,
                "1234.5678",
                f"/tmp/{file_id}.mp4",
                "source",
                "video/mp4",
                1234,
                None,
                None,
                None,
                status,
                {},
            ),
        )

    sources = await get_source_assets(engine, draft_id)

    assert [asset.slack_file_id for asset in sources] == ["Fgood"]
