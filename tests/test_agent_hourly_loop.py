"""Hourly Loop A — heartbeat, KPI, and isolated Meta collection."""

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from peermarket_agent.agent.loops.hourly import collect_meta_performance, run_hourly_pulse
from peermarket_agent.config import Settings
from peermarket_agent.db.migrations import run_migrations
from peermarket_agent.db.seed import seed


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


async def test_hourly_pulse_writes_heartbeat_row(engine):
    fake_peermarket = AsyncMock()
    fake_peermarket.fetch_kpis.return_value = {"signups": 3, "listings": 7}

    await run_hourly_pulse(engine=engine, peermarket=fake_peermarket)

    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT source, metric_name, value FROM kpis_hourly ORDER BY metric_name")
            )
        ).fetchall()
    metrics = {(r[0], r[1]): float(r[2]) for r in rows}
    assert metrics[("agent-internal", "heartbeat")] == 1.0
    assert metrics[("peermarket-prod", "signups")] == 3.0
    assert metrics[("peermarket-prod", "listings")] == 7.0


async def test_hourly_pulse_writes_heartbeat_when_peermarket_unavailable(engine):
    fake_peermarket = AsyncMock()
    fake_peermarket.fetch_kpis.side_effect = RuntimeError("connection refused")

    await run_hourly_pulse(engine=engine, peermarket=fake_peermarket)

    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT source, metric_name FROM kpis_hourly"))).fetchall()
    sources = {(r[0], r[1]) for r in rows}
    # heartbeat still written; peermarket metrics absent
    assert ("agent-internal", "heartbeat") in sources
    assert ("peermarket-prod", "signups") not in sources


NOW = datetime(2026, 7, 16, 14, tzinfo=UTC)


async def _publication(engine, draft_id, ad_id, *, published_at=None):
    published_at = published_at or datetime(2026, 7, 16, 10, tzinfo=UTC)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO drafts (id, action_type_id, channel, language, status) "
                "VALUES (:id, 1, 'meta', 'en', 'published')"
            ),
            {"id": draft_id},
        )
        await conn.execute(
            text(
                "INSERT INTO publications "
                "(draft_id, channel, state, external_ids, published_at) "
                "VALUES (:id, 'meta', 'active', CAST(:ids AS JSONB), :published_at)"
            ),
            {"id": draft_id, "ids": f'{{"ad_id":"{ad_id}"}}', "published_at": published_at},
        )


async def _attribution_alert(engine):
    async with engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT state, claim FROM operational_alert_state "
                        "WHERE alert_key='aggregate_attribution_availability'"
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
    return dict(row or {"state": {}, "claim": {}})


def _settings(**overrides):
    values = dict(
        meta_insights_enabled=True,
        peermarket_attribution_enabled=False,
        meta_app_id="app",
        meta_app_secret="secret",
        meta_system_user_token="token",
        meta_ad_account_id="act_1",
        meta_page_id="page",
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _snapshot(ad_id, impressions=10):
    return SimpleNamespace(
        ad_id=ad_id,
        window_start=NOW.date(),
        window_stop=NOW.date(),
        retrieved_at=NOW,
        spend_cents=100,
        impressions=impressions,
        reach=8,
        clicks=2,
        inline_link_clicks=2,
        outbound_clicks=2,
        landing_page_views=1,
        ctr=None,
        cpc_cents=50,
        cpm_cents=10000,
        frequency=None,
        actions={},
    )


ACTIVE = {
    "campaign": {"status": "ACTIVE", "effective_status": "ACTIVE"},
    "ad_set": {"status": "ACTIVE", "effective_status": "ACTIVE"},
    "ad": {"status": "ACTIVE", "effective_status": "ACTIVE"},
}


async def test_one_meta_failure_does_not_block_other_publications(engine, monkeypatch):
    await _publication(engine, 156, "ad-1")
    await _publication(engine, 157, "ad-2")
    statuses = AsyncMock(return_value=ACTIVE)

    async def insights(config, ad_id, start, stop):
        if ad_id == "ad-1":
            raise RuntimeError("token-secret must not be stored")
        return _snapshot(ad_id)

    monkeypatch.setattr("peermarket_agent.agent.loops.hourly.get_meta_ad_statuses", statuses)
    monkeypatch.setattr("peermarket_agent.agent.loops.hourly.fetch_meta_insights", insights)

    result = await collect_meta_performance(engine, _settings(), AsyncMock(), AsyncMock(), now=NOW)

    assert result.failed == [156]
    assert result.updated == [157]
    async with engine.connect() as conn:
        failure = (
            await conn.execute(text("SELECT performance FROM publications WHERE draft_id=156"))
        ).scalar_one()
    assert failure["meta"]["error"] == "Meta performance collection failed"
    assert "token-secret" not in str(failure)


async def test_hourly_snapshot_persists_explicit_requested_window_definition(engine, monkeypatch):
    await _publication(engine, 155, "ad-0")
    monkeypatch.setattr(
        "peermarket_agent.agent.loops.hourly.get_meta_ad_statuses",
        AsyncMock(return_value=ACTIVE),
    )
    monkeypatch.setattr(
        "peermarket_agent.agent.loops.hourly.fetch_meta_insights",
        AsyncMock(return_value=_snapshot("ad-0")),
    )

    await collect_meta_performance(engine, _settings(), AsyncMock(), AsyncMock(), now=NOW)

    async with engine.connect() as conn:
        performance = (
            await conn.execute(text("SELECT performance FROM publications WHERE draft_id=155"))
        ).scalar_one()
    assert performance["meta"]["latest"]["window_definition"] == (
        "rolling-3-inclusive-calendar-days"
    )


async def test_no_delivery_alert_is_deduplicated_and_recovers_once(engine, monkeypatch):
    await _publication(engine, 156, "ad-1")
    notifier = AsyncMock()
    notifier.notify_founder.return_value = True
    monkeypatch.setattr(
        "peermarket_agent.agent.loops.hourly.get_meta_ad_statuses", AsyncMock(return_value=ACTIVE)
    )
    insights = AsyncMock(return_value=_snapshot("ad-1", impressions=0))
    monkeypatch.setattr("peermarket_agent.agent.loops.hourly.fetch_meta_insights", insights)

    await collect_meta_performance(engine, _settings(), AsyncMock(), notifier, now=NOW)
    await collect_meta_performance(engine, _settings(), AsyncMock(), notifier, now=NOW)
    assert notifier.notify_founder.await_count == 1

    insights.return_value = _snapshot("ad-1", impressions=10)
    await collect_meta_performance(engine, _settings(), AsyncMock(), notifier, now=NOW)
    await collect_meta_performance(engine, _settings(), AsyncMock(), notifier, now=NOW)
    assert notifier.notify_founder.await_count == 2
    assert "recovered" in notifier.notify_founder.await_args.args[0].lower()


async def test_feature_flags_default_false_and_disabled_pulse_does_not_collect(engine, monkeypatch):
    assert Settings.model_fields["meta_insights_enabled"].default is False
    assert Settings.model_fields["peermarket_attribution_enabled"].default is False
    collector = AsyncMock()
    monkeypatch.setattr("peermarket_agent.agent.loops.hourly.collect_meta_performance", collector)
    peermarket = AsyncMock()
    peermarket.fetch_kpis.return_value = {}

    await run_hourly_pulse(engine, peermarket, settings=_settings(meta_insights_enabled=False))

    collector.assert_not_awaited()


async def test_missing_attribution_view_alerts_once_without_blocking_meta(engine, monkeypatch):
    await _publication(engine, 156, "ad-1")
    peermarket = AsyncMock()
    peermarket.fetch_attribution.side_effect = PermissionError("password=secret")
    notifier = AsyncMock()
    notifier.notify_founder.return_value = True
    monkeypatch.setattr(
        "peermarket_agent.agent.loops.hourly.get_meta_ad_statuses", AsyncMock(return_value=ACTIVE)
    )
    monkeypatch.setattr(
        "peermarket_agent.agent.loops.hourly.fetch_meta_insights",
        AsyncMock(return_value=_snapshot("ad-1")),
    )
    settings = _settings(peermarket_attribution_enabled=True)

    first = await collect_meta_performance(engine, settings, peermarket, notifier, now=NOW)
    second = await collect_meta_performance(engine, settings, peermarket, notifier, now=NOW)

    assert first.updated == [156]
    assert second.updated == [156]
    assert notifier.notify_founder.await_count == 1
    async with engine.connect() as conn:
        performance = (
            await conn.execute(text("SELECT performance FROM publications WHERE draft_id=156"))
        ).scalar_one()
    assert performance["attribution"]["error"] == "Aggregate attribution unavailable"
    assert "secret" not in str(performance)


async def test_concurrent_collectors_claim_one_problem_sender(engine, monkeypatch):
    await _publication(engine, 156, "ad-1")
    monkeypatch.setattr(
        "peermarket_agent.agent.loops.hourly.get_meta_ad_statuses",
        AsyncMock(return_value=ACTIVE),
    )
    monkeypatch.setattr(
        "peermarket_agent.agent.loops.hourly.fetch_meta_insights",
        AsyncMock(return_value=_snapshot("ad-1", impressions=0)),
    )
    notifier = AsyncMock()

    async def delivered(message):
        await asyncio.sleep(0.05)
        return True

    notifier.notify_founder.side_effect = delivered

    await asyncio.gather(
        collect_meta_performance(engine, _settings(), AsyncMock(), notifier, now=NOW),
        collect_meta_performance(engine, _settings(), AsyncMock(), notifier, now=NOW),
    )

    notifier.notify_founder.assert_awaited_once()


@pytest.mark.parametrize("first_result", [False, RuntimeError("slack down")])
async def test_problem_alert_delivery_failure_remains_retryable(engine, monkeypatch, first_result):
    await _publication(engine, 156, "ad-1")
    monkeypatch.setattr(
        "peermarket_agent.agent.loops.hourly.get_meta_ad_statuses",
        AsyncMock(return_value=ACTIVE),
    )
    monkeypatch.setattr(
        "peermarket_agent.agent.loops.hourly.fetch_meta_insights",
        AsyncMock(return_value=_snapshot("ad-1", impressions=0)),
    )
    notifier = AsyncMock()
    if isinstance(first_result, Exception):
        notifier.notify_founder.side_effect = [first_result, True]
    else:
        notifier.notify_founder.side_effect = [first_result, True]

    await collect_meta_performance(engine, _settings(), AsyncMock(), notifier, now=NOW)
    async with engine.connect() as conn:
        after_failure = (
            await conn.execute(text("SELECT performance FROM publications WHERE draft_id=156"))
        ).scalar_one()
    assert "alert_state" not in after_failure
    assert "alert_claim" not in after_failure
    await collect_meta_performance(engine, _settings(), AsyncMock(), notifier, now=NOW)

    assert notifier.notify_founder.await_count == 2
    async with engine.connect() as conn:
        performance = (
            await conn.execute(text("SELECT performance FROM publications WHERE draft_id=156"))
        ).scalar_one()
    assert performance["alert_state"]["active"] is True
    assert "claim_token" not in performance.get("alert_claim", {})


async def test_stale_problem_claim_can_be_reclaimed(engine, monkeypatch):
    await _publication(engine, 156, "ad-1")
    stale = {
        "alert_claim": {
            "claim_token": "crashed-worker",
            "claimed_at": (NOW - timedelta(minutes=10)).isoformat(),
            "condition": "no_delivery",
            "observed_state": ACTIVE,
            "active": True,
        }
    }
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE publications SET performance=CAST(:p AS JSONB) WHERE draft_id=156"),
            {"p": json.dumps(stale)},
        )
    monkeypatch.setattr(
        "peermarket_agent.agent.loops.hourly.get_meta_ad_statuses",
        AsyncMock(return_value=ACTIVE),
    )
    monkeypatch.setattr(
        "peermarket_agent.agent.loops.hourly.fetch_meta_insights",
        AsyncMock(return_value=_snapshot("ad-1", impressions=0)),
    )
    notifier = AsyncMock()
    notifier.notify_founder.return_value = True

    await collect_meta_performance(engine, _settings(), AsyncMock(), notifier, now=NOW)

    notifier.notify_founder.assert_awaited_once()


@pytest.mark.parametrize("recovery_result", [False, RuntimeError("slack down")])
async def test_recovery_delivery_failure_remains_retryable(engine, monkeypatch, recovery_result):
    await _publication(engine, 156, "ad-1")
    monkeypatch.setattr(
        "peermarket_agent.agent.loops.hourly.get_meta_ad_statuses",
        AsyncMock(return_value=ACTIVE),
    )
    insights = AsyncMock(return_value=_snapshot("ad-1", impressions=0))
    monkeypatch.setattr("peermarket_agent.agent.loops.hourly.fetch_meta_insights", insights)
    notifier = AsyncMock()
    notifier.notify_founder.side_effect = [True, recovery_result, True]

    await collect_meta_performance(engine, _settings(), AsyncMock(), notifier, now=NOW)
    insights.return_value = _snapshot("ad-1", impressions=10)
    await collect_meta_performance(engine, _settings(), AsyncMock(), notifier, now=NOW)
    async with engine.connect() as conn:
        after_failure = (
            await conn.execute(text("SELECT performance FROM publications WHERE draft_id=156"))
        ).scalar_one()
    assert after_failure["alert_state"]["active"] is True
    assert "alert_claim" not in after_failure
    await collect_meta_performance(engine, _settings(), AsyncMock(), notifier, now=NOW)

    assert notifier.notify_founder.await_count == 3
    async with engine.connect() as conn:
        performance = (
            await conn.execute(text("SELECT performance FROM publications WHERE draft_id=156"))
        ).scalar_one()
    assert performance["alert_state"]["active"] is False


@pytest.mark.parametrize("first_result", [False, RuntimeError("slack down")])
async def test_attribution_alert_delivery_failure_remains_retryable(
    engine, monkeypatch, first_result
):
    await _publication(engine, 156, "ad-1")
    peermarket = AsyncMock()
    peermarket.fetch_attribution.side_effect = PermissionError("denied")
    monkeypatch.setattr(
        "peermarket_agent.agent.loops.hourly.get_meta_ad_statuses",
        AsyncMock(return_value=ACTIVE),
    )
    monkeypatch.setattr(
        "peermarket_agent.agent.loops.hourly.fetch_meta_insights",
        AsyncMock(return_value=_snapshot("ad-1")),
    )
    notifier = AsyncMock()
    notifier.notify_founder.side_effect = [first_result, True]
    settings = _settings(peermarket_attribution_enabled=True)

    await collect_meta_performance(engine, settings, peermarket, notifier, now=NOW)
    after_failure = await _attribution_alert(engine)
    assert after_failure["state"] == {}
    assert after_failure["claim"] == {}
    await collect_meta_performance(engine, settings, peermarket, notifier, now=NOW)

    assert notifier.notify_founder.await_count == 2


async def test_concurrent_collectors_claim_one_attribution_sender(engine, monkeypatch):
    await _publication(engine, 156, "ad-1")
    peermarket = AsyncMock()
    peermarket.fetch_attribution.side_effect = PermissionError("denied")
    monkeypatch.setattr(
        "peermarket_agent.agent.loops.hourly.get_meta_ad_statuses",
        AsyncMock(return_value=ACTIVE),
    )
    monkeypatch.setattr(
        "peermarket_agent.agent.loops.hourly.fetch_meta_insights",
        AsyncMock(return_value=_snapshot("ad-1")),
    )
    notifier = AsyncMock()

    async def delivered(message):
        await asyncio.sleep(0.05)
        return True

    notifier.notify_founder.side_effect = delivered
    settings = _settings(peermarket_attribution_enabled=True)

    await asyncio.gather(
        collect_meta_performance(engine, settings, peermarket, notifier, now=NOW),
        collect_meta_performance(engine, settings, peermarket, notifier, now=NOW),
    )

    notifier.notify_founder.assert_awaited_once()


async def test_attribution_outage_survives_publication_lifecycle_and_recovers_once(
    engine, monkeypatch
):
    await _publication(engine, 156, "ad-1")
    await _publication(engine, 157, "ad-2")
    peermarket = AsyncMock()
    peermarket.fetch_attribution.side_effect = PermissionError("denied")
    monkeypatch.setattr(
        "peermarket_agent.agent.loops.hourly.get_meta_ad_statuses",
        AsyncMock(return_value=ACTIVE),
    )
    monkeypatch.setattr(
        "peermarket_agent.agent.loops.hourly.fetch_meta_insights",
        AsyncMock(side_effect=lambda config, ad_id, start, stop: _snapshot(ad_id)),
    )
    notifier = AsyncMock()
    notifier.notify_founder.return_value = True
    settings = _settings(peermarket_attribution_enabled=True)

    await collect_meta_performance(engine, settings, peermarket, notifier, now=NOW)
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE publications SET state='terminal' WHERE draft_id=156"))
    await collect_meta_performance(engine, settings, peermarket, notifier, now=NOW)

    notifier.notify_founder.assert_awaited_once()

    peermarket.fetch_attribution.side_effect = None
    peermarket.fetch_attribution.return_value = []
    await collect_meta_performance(engine, settings, peermarket, notifier, now=NOW)
    await collect_meta_performance(engine, settings, peermarket, notifier, now=NOW)

    assert notifier.notify_founder.await_count == 2
    assert "recovered" in notifier.notify_founder.await_args.args[0].lower()
