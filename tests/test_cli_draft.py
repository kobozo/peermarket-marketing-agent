"""CLI draft command tests — end-to-end orchestration."""

import os
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from peermarket_agent.agent.cli_draft import run_draft_command
from peermarket_agent.claude import ClaudeResponse
from peermarket_agent.db.migrations import run_migrations
from peermarket_agent.db.seed import seed
from peermarket_agent.prompts.brand_voice import sync_to_db


@pytest.fixture
async def prepared_db():
    url = os.environ["AGENT_DB_URL"]
    eng = create_async_engine(url, future=True)
    async with eng.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await run_migrations(eng)
    await seed(eng)
    await sync_to_db(eng)
    yield eng
    await eng.dispose()


async def test_run_draft_tiktok_persists_high_score_draft(prepared_db):
    fake_claude = AsyncMock()
    fake_claude.complete = AsyncMock(
        side_effect=[
            ClaudeResponse(
                text='{"hook": "Wil je vandaag veilig en lokaal spullen verkopen?", "body": "Verkoop veilig op PeerMarket.", "cta": "Plaats het nu"}',
                input_tokens=200,
                output_tokens=40,
                model="claude-sonnet-4-6",
                stop_reason="end_turn",
            ),
            ClaudeResponse(
                text='{"score": 92, "notes": "Spot on."}',
                input_tokens=300,
                output_tokens=20,
                model="claude-sonnet-4-6",
                stop_reason="end_turn",
            ),
        ]
    )

    draft_id = await run_draft_command(
        engine=prepared_db,
        claude=fake_claude,
        action_type_name="tiktok_post_organic",
        language="NL",
        theme="declutter",
    )
    assert draft_id is not None

    async with prepared_db.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT copy, brand_score, channel, language, status FROM drafts WHERE id = :id"
                ),
                {"id": draft_id},
            )
        ).fetchone()
    assert "Wil je vandaag veilig" in row[0]
    assert row[1] == 92
    assert row[2] == "tiktok"
    assert row[3] == "NL"
    assert row[4] == "queued"


async def test_run_draft_rejects_low_score_draft_does_not_persist(prepared_db):
    fake_claude = AsyncMock()
    fake_claude.complete = AsyncMock(
        side_effect=[
            ClaudeResponse(
                text='{"hook": "Buy everything today with this amazing sales offer", "body": "amazing offer!!!", "cta": "buy it now"}',
                input_tokens=200,
                output_tokens=40,
                model="claude-sonnet-4-6",
                stop_reason="end_turn",
            ),
            ClaudeResponse(
                text='{"score": 45, "notes": "off-brand exclamation, hype phrases."}',
                input_tokens=300,
                output_tokens=20,
                model="claude-sonnet-4-6",
                stop_reason="end_turn",
            ),
        ]
    )

    draft_id = await run_draft_command(
        engine=prepared_db,
        claude=fake_claude,
        action_type_name="tiktok_post_organic",
        language="NL",
        theme="declutter",
    )
    assert draft_id is None

    async with prepared_db.connect() as conn:
        count = (await conn.execute(text("SELECT count(*) FROM drafts"))).scalar()
    assert count == 0


async def test_run_draft_email_persists(prepared_db):
    fake_claude = AsyncMock()
    fake_claude.complete = AsyncMock(
        side_effect=[
            ClaudeResponse(
                text='{"subject": "Je hebt nog niets verkocht", "body": "' + "woord " * 80 + '"}',
                input_tokens=250,
                output_tokens=80,
                model="claude-sonnet-4-6",
                stop_reason="end_turn",
            ),
            ClaudeResponse(
                text='{"score": 85, "notes": "Good."}',
                input_tokens=300,
                output_tokens=20,
                model="claude-sonnet-4-6",
                stop_reason="end_turn",
            ),
        ]
    )

    draft_id = await run_draft_command(
        engine=prepared_db,
        claude=fake_claude,
        action_type_name="email_re_engagement",
        language="NL",
        audience="dormant_signups",
    )
    assert draft_id is not None


async def test_run_draft_seo_persists(prepared_db):
    fake_claude = AsyncMock()
    fake_claude.complete = AsyncMock(
        side_effect=[
            ClaudeResponse(
                text=(
                    '{"title": "Veilig tweedehands kopen — PeerMarket",'
                    ' "description": "Belgische marktplaats met geverifieerde verkopers. '
                    'Plaats je eerste item gratis."}'
                ),
                input_tokens=200,
                output_tokens=40,
                model="claude-sonnet-4-6",
                stop_reason="end_turn",
            ),
            ClaudeResponse(
                text='{"score": 88, "notes": "Good."}',
                input_tokens=300,
                output_tokens=20,
                model="claude-sonnet-4-6",
                stop_reason="end_turn",
            ),
        ]
    )
    draft_id = await run_draft_command(
        engine=prepared_db,
        claude=fake_claude,
        action_type_name="seo_pr",
        language="NL",
        page_path="/about",
        page_subject="who we are",
    )
    assert draft_id is not None


async def test_run_draft_meta_persists_metadata(prepared_db):
    """meta_ad_creative drafts should have structured metadata in DB."""
    fake_claude = AsyncMock()
    fake_claude.complete = AsyncMock(
        side_effect=[
            ClaudeResponse(
                text=(
                    '{"primary_text": "PeerMarket is de Belgische marktplaats waar elke verkoper '
                    "zijn identiteit verifieert. Verkoop veilig en koop met vertrouwen. Plaats "
                    'vandaag je eerste item gratis.",'
                    ' "headline": "Verkoop veilig",'
                    ' "description": "Geverifieerde verkopers",'
                    ' "cta_label": "Learn More",'
                    ' "suggested_daily_budget_eur": 10}'
                ),
                input_tokens=300,
                output_tokens=120,
                model="claude-sonnet-4-6",
                stop_reason="end_turn",
            ),
            ClaudeResponse(
                text='{"score": 90, "notes": "Spot on."}',
                input_tokens=300,
                output_tokens=20,
                model="claude-sonnet-4-6",
                stop_reason="end_turn",
            ),
        ]
    )

    draft_id = await run_draft_command(
        engine=prepared_db,
        claude=fake_claude,
        action_type_name="meta_ad_creative",
        language="NL",
        audience_profile_key="declutterers",
    )
    assert draft_id is not None

    async with prepared_db.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT channel, metadata FROM drafts WHERE id = :id"),
                {"id": draft_id},
            )
        ).fetchone()
    assert row[0] == "meta"
    meta = row[1]
    assert meta["audience_profile_key"] == "declutterers"
    assert meta["objective"] == "OUTCOME_TRAFFIC"
    assert meta["headline"] == "Verkoop veilig"
    assert meta["cta_label"] == "Learn More"
    assert meta["cta_type"] == "LEARN_MORE"
    assert meta["suggested_daily_budget_eur"] == 10
    assert "PeerMarket is de Belgische" in meta["primary_text"]


async def test_meta_generation_uses_only_five_recent_relevant_learnings(prepared_db):
    async with prepared_db.begin() as conn:
        for index in range(7):
            await conn.execute(
                text("INSERT INTO learnings (scope, text) VALUES (:scope, :learning)"),
                {
                    "scope": "delivery:meta:OUTCOME_TRAFFIC:NL:declutterers:rolling-3",
                    "learning": f"relevant-{index}",
                },
            )
        await conn.execute(
            text("INSERT INTO learnings (scope, text) VALUES (:scope, 'do-not-use')"),
            {"scope": "delivery:meta:OUTCOME_TRAFFIC:FR:declutterers:rolling-3"},
        )
    fake_claude = AsyncMock()
    fake_claude.complete = AsyncMock(
        side_effect=[
            ClaudeResponse(
                text=(
                    '{"primary_text": "PeerMarket is de Belgische marktplaats waar elke verkoper '
                    "zijn identiteit verifieert. Verkoop veilig en koop met vertrouwen. Plaats "
                    'vandaag je eerste item gratis.", "headline": "Verkoop veilig", '
                    '"description": "Geverifieerde verkopers", "cta_label": "Learn More", '
                    '"suggested_daily_budget_eur": 10}'
                ),
                input_tokens=10,
                output_tokens=10,
                model="claude-sonnet-4-6",
                stop_reason="end_turn",
            ),
            ClaudeResponse(
                text='{"score": 90, "notes": "Good."}',
                input_tokens=10,
                output_tokens=10,
                model="claude-sonnet-4-6",
                stop_reason="end_turn",
            ),
        ]
    )

    await run_draft_command(
        engine=prepared_db,
        claude=fake_claude,
        action_type_name="meta_ad_creative",
        language="NL",
        audience_profile_key="declutterers",
    )

    prompt = fake_claude.complete.await_args_list[0].kwargs["user"]
    assert "relevant-6" in prompt and "relevant-2" in prompt
    assert "relevant-1" not in prompt and "relevant-0" not in prompt
    assert "do-not-use" not in prompt


async def test_run_draft_credit_low_dms_founder_and_reraises(prepared_db):
    """Anthropic credit-low errors trigger Slack DM and propagate."""
    import httpx
    from anthropic import BadRequestError

    from peermarket_agent.balance_alert import NUDGE_MESSAGE

    fake_claude = AsyncMock()
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(
        status_code=400,
        request=request,
        json={
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "message": "Your credit balance is too low",
            },
        },
    )
    fake_claude.complete = AsyncMock(
        side_effect=BadRequestError(
            message="Your credit balance is too low",
            response=response,
            body={"error": {"message": "Your credit balance is too low"}},
        )
    )

    fake_notifier = AsyncMock()
    fake_notifier.notify_founder = AsyncMock(return_value=True)

    with pytest.raises(BadRequestError):
        await run_draft_command(
            engine=prepared_db,
            claude=fake_claude,
            notifier=fake_notifier,
            action_type_name="tiktok_post_organic",
            language="NL",
            theme="declutter",
        )
    fake_notifier.notify_founder.assert_awaited_once_with(NUDGE_MESSAGE)
