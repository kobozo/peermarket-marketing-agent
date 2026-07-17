"""Daily Loop B tests."""

import json
import os
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from peermarket_agent.agent.loops.daily import (
    _seconds_until_next_9am,
    run_daily_drafts,
)
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


async def test_seconds_until_next_9am_returns_positive_float():
    secs = await _seconds_until_next_9am()
    assert secs > 0
    assert secs <= 24 * 3600  # never more than a day


async def test_run_daily_drafts_dms_persisted_drafts_and_summary(prepared_db):
    # 3 action types × 2 calls each (generator + brand_quality gate) = 6 calls.
    # Order matches _TODAYS_PLAN: meta, tiktok, email.
    fake_claude = AsyncMock()
    fake_claude.complete = AsyncMock(
        side_effect=[
            # 1) Meta gen
            ClaudeResponse(
                text=(
                    '{"primary_text": "' + "x" * 150 + '", "headline": "ok", '
                    '"description": "ok", "cta_label": "Learn More", '
                    '"suggested_daily_budget_eur": 10}'
                ),
                input_tokens=400,
                output_tokens=80,
                model="claude-sonnet-4-6",
                stop_reason="end_turn",
            ),
            # 2) Meta brand-quality
            ClaudeResponse(
                text='{"score": 92, "notes": "ok"}',
                input_tokens=300,
                output_tokens=20,
                model="claude-sonnet-4-6",
                stop_reason="end_turn",
            ),
            # 3) TikTok gen
            ClaudeResponse(
                text=(
                    '{"hook": "Wil je vandaag veilig en lokaal spullen verkopen?", '
                    '"body": "Veilig verkopen.", "cta": "Plaats het nu", '
                    '"script": "Verkoop lokaal en veilig via PeerMarket.", '
                    '"shots": ["Toon het item", "Open PeerMarket"], '
                    '"on_screen_text": ["Veilig lokaal verkopen"], '
                    '"recording_notes": "Film verticaal bij daglicht."}'
                ),
                input_tokens=200,
                output_tokens=40,
                model="claude-sonnet-4-6",
                stop_reason="end_turn",
            ),
            # 4) TikTok brand-quality
            ClaudeResponse(
                text='{"score": 88, "notes": "ok"}',
                input_tokens=300,
                output_tokens=20,
                model="claude-sonnet-4-6",
                stop_reason="end_turn",
            ),
            # 5) Email gen
            ClaudeResponse(
                text=json.dumps(
                    {"subject": "Je hebt nog niets verkocht", "body": "woord " * 80}
                ),
                input_tokens=250,
                output_tokens=80,
                model="claude-sonnet-4-6",
                stop_reason="end_turn",
            ),
            # 6) Email brand-quality
            ClaudeResponse(
                text='{"score": 90, "notes": "ok"}',
                input_tokens=300,
                output_tokens=20,
                model="claude-sonnet-4-6",
                stop_reason="end_turn",
            ),
        ]
    )

    fake_notifier = AsyncMock()
    fake_notifier.notify_founder = AsyncMock(return_value=True)

    persisted = await run_daily_drafts(
        engine=prepared_db, claude=fake_claude, notifier=fake_notifier
    )
    assert persisted == 3
    # Approval roots are persisted to the outbox; only the operational summary is immediate.
    assert fake_notifier.notify_founder.await_count == 1
    async with prepared_db.connect() as conn:
        queued = (
            await conn.execute(
                text("SELECT count(*) FROM slack_outbox WHERE message_kind='root_approval'")
            )
        ).scalar_one()
    assert queued == 3

    # KPI row recorded
    async with prepared_db.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT value FROM kpis_hourly "
                    "WHERE metric_name='daily_drafts_generated' "
                    "ORDER BY ts DESC LIMIT 1"
                )
            )
        ).fetchone()
    assert row[0] == 3


async def test_run_daily_drafts_skips_gate_rejections(prepared_db):
    fake_claude = AsyncMock()
    fake_claude.complete = AsyncMock(
        side_effect=[
            # Meta gen ok, gate REJECTS
            ClaudeResponse(
                text=(
                    '{"primary_text": "' + "x" * 150 + '", "headline": "ok", '
                    '"description": "ok", "cta_label": "Learn More", '
                    '"suggested_daily_budget_eur": 10}'
                ),
                input_tokens=400,
                output_tokens=80,
                model="claude-sonnet-4-6",
                stop_reason="end_turn",
            ),
            ClaudeResponse(
                text='{"score": 50, "notes": "off-brand"}',
                input_tokens=300,
                output_tokens=20,
                model="claude-sonnet-4-6",
                stop_reason="end_turn",
            ),
            # TikTok ok
            ClaudeResponse(
                text=(
                    '{"hook": "Wil je vandaag veilig en lokaal spullen verkopen?", '
                    '"body": "Veilig verkopen.", "cta": "Plaats het nu", '
                    '"script": "Verkoop lokaal en veilig via PeerMarket.", '
                    '"shots": ["Toon het item", "Open PeerMarket"], '
                    '"on_screen_text": ["Veilig lokaal verkopen"], '
                    '"recording_notes": "Film verticaal bij daglicht."}'
                ),
                input_tokens=200,
                output_tokens=40,
                model="claude-sonnet-4-6",
                stop_reason="end_turn",
            ),
            ClaudeResponse(
                text='{"score": 88, "notes": "ok"}',
                input_tokens=300,
                output_tokens=20,
                model="claude-sonnet-4-6",
                stop_reason="end_turn",
            ),
            # Email ok
            ClaudeResponse(
                text=('{"subject": "ok", "body": "' + "woord " * 80 + '"}'),
                input_tokens=250,
                output_tokens=80,
                model="claude-sonnet-4-6",
                stop_reason="end_turn",
            ),
            ClaudeResponse(
                text='{"score": 88, "notes": "ok"}',
                input_tokens=300,
                output_tokens=20,
                model="claude-sonnet-4-6",
                stop_reason="end_turn",
            ),
        ]
    )
    fake_notifier = AsyncMock()
    fake_notifier.notify_founder = AsyncMock(return_value=True)

    persisted = await run_daily_drafts(
        engine=prepared_db, claude=fake_claude, notifier=fake_notifier
    )
    assert persisted == 2
    # 2 approval roots queued + 1 operational summary sent best-effort.
    assert fake_notifier.notify_founder.await_count == 1
    async with prepared_db.connect() as conn:
        queued = (await conn.execute(text("SELECT count(*) FROM slack_outbox"))).scalar_one()
    assert queued == 2


async def test_daily_metric_counts_ready_approval_when_enqueue_is_idempotent(
    prepared_db, monkeypatch
):
    monkeypatch.setattr(
        "peermarket_agent.agent.loops.daily._TODAYS_PLAN",
        [{"action_type_name": "tiktok_post_organic", "language": "NL"}],
    )
    draft_id = 77
    monkeypatch.setattr(
        "peermarket_agent.agent.loops.daily.run_draft_command",
        AsyncMock(return_value=draft_id),
    )
    monkeypatch.setattr(
        "peermarket_agent.agent.loops.daily._fetch_draft_with_action_name",
        AsyncMock(
            return_value={
                "id": draft_id,
                "action_type_name": "tiktok_post_organic",
                "language": "NL",
                "channel": "tiktok",
                "brand_score": 90,
                "copy": "ready",
            }
        ),
    )
    monkeypatch.setattr(
        "peermarket_agent.agent.loops.daily.enqueue_root_approval",
        AsyncMock(return_value=False),
    )
    notifier = AsyncMock()
    notifier.notify_founder = AsyncMock(return_value=True)

    assert await run_daily_drafts(engine=prepared_db, claude=AsyncMock(), notifier=notifier) == 1
    assert "1/1" in notifier.notify_founder.await_args.args[0]
