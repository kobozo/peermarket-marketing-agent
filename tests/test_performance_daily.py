import os
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from peermarket_agent.agent.loops.performance_daily import (
    evaluate_publication,
    run_daily_performance,
    safe_ratio,
)
from peermarket_agent.db.migrations import run_migrations
from peermarket_agent.db.seed import seed


@pytest.fixture
async def database_engine():
    engine = create_async_engine(os.environ["AGENT_DB_URL"], future=True)
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    yield engine
    await engine.dispose()


def test_missing_registration_data_is_unavailable_not_zero():
    observation = evaluate_publication(
        {"meta": {"latest": {"landing_page_views": 5}}, "attribution": {"available": False}}
    )
    assert observation.metrics["landing_to_registration"] is None
    assert observation.metrics["registrations"] is None


def test_safe_ratio_rejects_missing_and_zero_denominators():
    assert safe_ratio(None, 5) is None
    assert safe_ratio(2, None) is None
    assert safe_ratio(2, 0) is None
    assert str(safe_ratio(1, 4)) == "0.25"


def test_evaluator_uses_only_available_attributed_registration_events():
    observation = evaluate_publication(
        {
            "meta": {"latest": {"impressions": 1000, "landing_page_views": 40}},
            "attribution": {
                "available": True,
                "events": [
                    {"event_type": "registration", "event_count": 8},
                    {"event_type": "listing", "event_count": 3},
                ],
            },
        }
    )
    assert observation.metrics["registrations"] == 8
    assert str(observation.metrics["landing_to_registration"]) == "0.2"


async def _insert_publication(engine, *, draft_id, audience, performance, ads_url):
    async with engine.begin() as conn:
        action_type_id = (
            await conn.execute(text("SELECT id FROM action_types WHERE name='meta_ad_creative'"))
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO drafts "
                "(id, action_type_id, channel, language, metadata, status) "
                "VALUES (:id, :action, 'meta', 'NL', CAST(:metadata AS JSONB), 'published')"
            ),
            {
                "id": draft_id,
                "action": action_type_id,
                "metadata": '{"audience_profile_key":"' + audience + '"}',
            },
        )
        await conn.execute(
            text(
                "INSERT INTO publications "
                "(draft_id, channel, performance, ads_manager_url, published_at) "
                "VALUES (:id, 'meta', CAST(:performance AS JSONB), :url, :published)"
            ),
            {
                "id": draft_id,
                "performance": __import__("json").dumps(performance),
                "url": ads_url,
                "published": datetime(2026, 7, 14, tzinfo=UTC),
            },
        )


async def test_daily_run_is_idempotent_and_sanitizes_unavailable_summary(database_engine):
    await run_migrations(database_engine)
    await seed(database_engine)
    await _insert_publication(
        database_engine,
        draft_id=501,
        audience="declutterers",
        ads_url="https://business.facebook.com/adsmanager/manage/ads?act=123&selected_ad_ids=abc",
        performance={
            "meta": {
                "latest": {
                    "impressions": 1000,
                    "landing_page_views": 30,
                    "window_start": "2026-07-15",
                    "window_stop": "2026-07-16",
                }
            },
            "attribution": {"available": False, "error": "password=super-secret"},
        },
    )
    notifier = AsyncMock()
    notifier.notify_founder.return_value = True
    now = datetime(2026, 7, 16, 9, tzinfo=UTC)

    assert await run_daily_performance(database_engine, notifier, object(), now=now) == 1
    assert await run_daily_performance(database_engine, notifier, object(), now=now) == 0

    async with database_engine.connect() as conn:
        performance = (
            await conn.execute(text("SELECT performance FROM publications WHERE draft_id=501"))
        ).scalar_one()
    assert len(performance["daily_observations"]) == 1
    message = notifier.notify_founder.await_args_list[0].args[0]
    assert "unavailable" in message
    assert "2026-07-15 → 2026-07-16 UTC" in message
    assert "https://business.facebook.com/adsmanager/manage/ads" in message
    assert "super-secret" not in message
    assert "caused" not in message.lower()


async def test_daily_run_inserts_then_idempotently_reinforces_learning(database_engine):
    await run_migrations(database_engine)
    await seed(database_engine)
    base = {
        "meta": {
            "latest": {
                "impressions": 1000,
                "landing_page_views": 30,
                "window_start": "2026-07-15",
                "window_stop": "2026-07-16",
            }
        },
        "attribution": {
            "available": True,
            "events": [{"event_type": "registration", "event_count": 10}],
        },
    }
    await _insert_publication(
        database_engine, draft_id=601, audience="declutterers", performance=base, ads_url=None
    )
    await _insert_publication(
        database_engine, draft_id=602, audience="declutterers", performance=base, ads_url=None
    )
    notifier = AsyncMock()
    now = datetime(2026, 7, 16, 9, tzinfo=UTC)

    assert await run_daily_performance(database_engine, notifier, object(), now=now) == 2
    assert await run_daily_performance(database_engine, notifier, object(), now=now) == 0

    async with database_engine.connect() as conn:
        rows = (
            (await conn.execute(text("SELECT evidence_links, seen_n_times FROM learnings")))
            .mappings()
            .all()
        )
    assert len(rows) == 1
    assert rows[0]["seen_n_times"] == 1
    assert rows[0]["evidence_links"]["window"] == {
        "start": "2026-07-15",
        "stop": "2026-07-16",
        "definition": "utc-day",
    }
    assert rows[0]["evidence_links"]["sample"]["variants"] == 2
