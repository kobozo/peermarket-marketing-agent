"""Hourly Loop A — heartbeat, KPI, and isolated Meta collection."""

import asyncio
import json
import os
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from peermarket_agent.agent.loops.hourly import (
    _collect_hook_experiment_metrics,
    collect_meta_performance,
    run_hourly_pulse,
)
from peermarket_agent.autonomy.contracts import DecisionKind
from peermarket_agent.autonomy.snapshot import build_policy_decision
from peermarket_agent.config import Settings
from peermarket_agent.db.migrations import run_migrations
from peermarket_agent.db.seed import seed
from peermarket_agent.slack_blocks import hourly_alert_blocks


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


async def _publication(
    engine, draft_id, ad_id, *, published_at=None, external_ids=None, budget=None
):
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
                "(draft_id, channel, state, external_ids, approved_budget_cents, published_at) "
                "VALUES (:id, 'meta', 'active', CAST(:ids AS JSONB), :budget, :published_at)"
            ),
            {
                "id": draft_id,
                "ids": json.dumps(external_ids or {"ad_id": ad_id}),
                "budget": budget,
                "published_at": published_at,
            },
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


async def test_hook_experiment_collector_persists_real_nine_ad_metrics(engine, monkeypatch):
    experiment = "exp-hook"
    await _publication(
        engine,
        156,
        "source-ad",
        external_ids={"campaign_id": "10", "ad_set_id": "20", "ad_id": "source-ad"},
        budget=1000,
    )
    progress = {"campaign_id": "50", "ad_set_id": "60"}
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO drafts(id,action_type_id,channel,language,status) VALUES (157,1,'meta','MULTI','approved')"
            )
        )
        decision = await conn.scalar(
            text(
                "INSERT INTO autonomous_decisions(decision_key,kind,campaign_id,window_start,window_end,evidence,reason) VALUES ('hook-collect','replace','10',NOW()-INTERVAL '1 day',NOW(),'{}','test') RETURNING id"
            )
        )
        action = await conn.scalar(
            text(
                "INSERT INTO autonomous_actions(decision_id,campaign_id,status) VALUES (:decision,'10','executing') RETURNING id"
            ),
            {"decision": decision},
        )
        for number in (1, 2, 3):
            variant = f"{experiment}:{number:02}"
            for locale in ("NL", "FR", "EN"):
                ad_id = f"ad-{number}-{locale}"
                progress[f"variant:{variant}:ad_id:{locale}"] = ad_id
                await conn.execute(
                    text(
                        "INSERT INTO autonomous_hook_experiment_variants(experiment_id,variant_id,language,campaign_id,ad_set_id,landing_page_url,changed_dimension,fixed_identity,language_bundle) VALUES (:experiment,:variant,:locale,'10','60','https://peermarket.eu/','hook',CAST(:identity AS JSONB),CAST(:bundle AS JSONB))"
                    ),
                    {
                        "experiment": experiment,
                        "variant": variant,
                        "locale": locale,
                        "identity": json.dumps(
                            {
                                "audience": "declutterers",
                                "optimization": "LINK_CLICKS",
                                "format": "single_image",
                                "visual": "asset",
                                "delivery": "lowest_cost",
                            }
                        ),
                        "bundle": json.dumps(
                            {
                                "hook": "h",
                                "body": "b",
                                "headline": "x",
                                "description": "d",
                                "cta_label": "Learn More",
                            }
                        ),
                    },
                )
        await conn.execute(
            text(
                "INSERT INTO autonomous_replacement_publications(action_id,replacement_draft_id,source_draft_id,state,frozen_budget_cents,source_campaign_id,changed_dimension,landing_page_url,progress) VALUES (:action,157,156,'paused',1000,'10','hook','https://peermarket.eu/',CAST(:progress AS JSONB))"
            ),
            {"action": action, "progress": json.dumps(progress)},
        )

    monkeypatch.setattr(
        "peermarket_agent.agent.loops.hourly.fetch_meta_insights",
        AsyncMock(side_effect=lambda config, ad_id, start, stop: _snapshot(ad_id, impressions=100)),
    )
    await _collect_hook_experiment_metrics(
        engine,
        SimpleNamespace(meta_autonomy_experiment_id=experiment),
        object(),
        NOW.date(),
        NOW.date(),
        NOW - timedelta(days=1),
        NOW,
        tuple(
            SimpleNamespace(
                utm_content=f"{experiment}:{number:02}:{locale}",
                event_type="registration",
                event_count=number,
            )
            for number in (1, 2, 3)
            for locale in ("NL", "FR", "EN")
        ),
        NOW,
    )
    async with engine.connect() as conn:
        performance = await conn.scalar(
            text("SELECT performance FROM publications WHERE draft_id=156")
        )
    persisted = performance["hook_experiment_variants"]
    assert len(persisted) == 3
    assert (
        sum(item["impressions"] for variant in persisted.values() for item in variant.values())
        == 900
    )
    assert all(item["window_start"] for variant in persisted.values() for item in variant.values())
    assert [
        sum(item["registrations"] for item in persisted[f"{experiment}:{number:02}"].values())
        for number in (1, 2, 3)
    ] == [3, 6, 9]


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


async def test_collector_basis_flows_through_public_policy_builder(engine, monkeypatch):
    ids = {
        "campaign_id": "10",
        "ad_set_id": "20",
        "ad_id": "31",
        "creative_ids": {"NL": "41", "FR": "42", "EN": "43"},
    }
    await _publication(engine, 155, "31", external_ids=ids, budget=1000)
    monkeypatch.setattr(
        "peermarket_agent.agent.loops.hourly.get_meta_ad_statuses",
        AsyncMock(return_value=ACTIVE),
    )
    monkeypatch.setattr(
        "peermarket_agent.agent.loops.hourly.fetch_meta_insights",
        AsyncMock(return_value=_snapshot("31", impressions=2000)),
    )
    settings = _settings(peermarket_attribution_enabled=True)
    peermarket = AsyncMock()
    peermarket.fetch_attribution.return_value = []
    await collect_meta_performance(engine, settings, peermarket, AsyncMock(), now=NOW)
    async with engine.connect() as conn:
        publication = dict(
            (
                await conn.execute(
                    text(
                        "SELECT id AS publication_id,draft_id,external_ids,approved_budget_cents,performance FROM publications WHERE draft_id=155"
                    )
                )
            )
            .mappings()
            .one()
        )
    assert publication["performance"]["autonomy_basis"]["external_ids"] == ids
    variants = [
        {
            "variant_id": str(i),
            "publication_id": i,
            "channel": "meta",
            "objective": "OUTCOME_TRAFFIC",
            "language": "MULTI",
            "audience": "declutterers",
            "creative_dimension": "hook",
            "window_definition": "rolling-3-inclusive-calendar-days",
            "impressions": 1000,
            "landing_page_views": 30,
            "registrations": registrations,
        }
        for i, registrations in ((1, 20), (2, 10))
    ]
    source = {
        "draft_id": 155,
        "publication_id": publication["publication_id"],
        "campaign_id": "10",
        "experiment_id": "exp",
        "changed_dimension": "hook",
        "locales": {
            locale: {
                "locale": locale,
                "hook": "hook",
                "body": "body",
                "headline": "headline",
                "description": "description",
                "cta_label": "Learn More",
            }
            for locale in ("NL", "FR", "EN")
        },
        "audience_profile_key": "declutterers",
        "image_prompt": "real screenshot",
        "asset_path": "/tmp/source.png",
        "daily_budget_eur": 10,
        "landing_page_url": "https://peermarket.eu/",
        "objective": "OUTCOME_TRAFFIC",
        "current_meta_ids": {
            "campaign_id": "10",
            "ad_set_id": "20",
            "ad_ids": {"NL": "31", "FR": "32", "EN": "33"},
            "creative_ids": {"NL": "41", "FR": "42", "EN": "43"},
        },
    }
    limits = {
        "performance_snapshot_max_age_hours": 2,
        "learning_min_impressions": 1000,
        "learning_min_landing_page_views": 30,
        "learning_min_registrations": 10,
        "meta_autonomy_cooldown_hours": 24,
        "meta_autonomy_max_test_days": 7,
        "meta_autonomy_max_replacements_24h": 1,
        "meta_autonomy_max_increase_percent": 20,
        "meta_autonomy_max_daily_budget_eur": 20,
        "meta_no_delivery_grace_hours": 2,
        "meta_account_timezone": "Europe/Brussels",
    }
    decision = build_policy_decision(
        publication, variants, replacement_source=source, history=(), limits=limits, now=NOW
    )
    assert decision.kind is DecisionKind.REPLACE
    assert decision.evidence["frozen_basis"]["approved_budget_cents"] == 1000


async def test_collection_uses_configured_last_completed_account_days_and_utc_alignment(
    engine, monkeypatch
):
    await _publication(engine, 155, "ad-0")
    insights = AsyncMock(return_value=_snapshot("ad-0"))
    peermarket = AsyncMock()
    peermarket.fetch_attribution.return_value = []
    monkeypatch.setattr(
        "peermarket_agent.agent.loops.hourly.get_meta_ad_statuses",
        AsyncMock(return_value=ACTIVE),
    )
    monkeypatch.setattr("peermarket_agent.agent.loops.hourly.fetch_meta_insights", insights)
    settings = _settings(
        meta_insights_lookback_days=2,
        meta_account_timezone="America/New_York",
        peermarket_attribution_enabled=True,
    )

    await collect_meta_performance(
        engine, settings, peermarket, AsyncMock(), now=datetime(2026, 7, 16, 2, tzinfo=UTC)
    )

    assert insights.await_args.args[2:] == (date(2026, 7, 13), date(2026, 7, 14))
    peermarket.fetch_attribution.assert_awaited_once_with(date(2026, 7, 13), date(2026, 7, 15))
    async with engine.connect() as conn:
        latest = (
            await conn.execute(text("SELECT performance FROM publications WHERE draft_id=155"))
        ).scalar_one()["meta"]["latest"]
    assert latest["account_timezone"] == "America/New_York"
    assert latest["account_window"] == {"start": "2026-07-13", "stop": "2026-07-14"}
    assert latest["utc_alignment"] == {
        "start": "2026-07-13T04:00:00+00:00",
        "stop_exclusive": "2026-07-15T04:00:00+00:00",
        "overlap_start_day": "2026-07-13",
        "overlap_stop_day": "2026-07-15",
    }


async def test_collection_uses_configured_no_delivery_grace(engine, monkeypatch):
    await _publication(engine, 156, "ad-1")
    monkeypatch.setattr(
        "peermarket_agent.agent.loops.hourly.get_meta_ad_statuses",
        AsyncMock(return_value=ACTIVE),
    )
    monkeypatch.setattr(
        "peermarket_agent.agent.loops.hourly.fetch_meta_insights",
        AsyncMock(return_value=_snapshot("ad-1", impressions=0)),
    )

    await collect_meta_performance(
        engine,
        _settings(meta_no_delivery_grace_hours=48),
        AsyncMock(),
        AsyncMock(),
        now=NOW,
    )

    async with engine.connect() as conn:
        performance = (
            await conn.execute(text("SELECT performance FROM publications WHERE draft_id=156"))
        ).scalar_one()
    assert performance["delivery"]["condition"] == "unknown"


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


async def test_claimed_alert_routes_to_report_channel_with_blocks(engine, monkeypatch):
    await _publication(engine, 156, "ad-1")
    monkeypatch.setattr(
        "peermarket_agent.agent.loops.hourly.get_meta_ad_statuses", AsyncMock(return_value=ACTIVE)
    )
    monkeypatch.setattr(
        "peermarket_agent.agent.loops.hourly.fetch_meta_insights",
        AsyncMock(return_value=_snapshot("ad-1", impressions=0)),
    )
    notifier = AsyncMock()
    settings = _settings(slack_report_channel_meta="C0BJ0PUURRR")

    await collect_meta_performance(engine, settings, AsyncMock(), notifier, now=NOW)

    notifier.send_message.assert_awaited_once()
    notifier.notify_founder.assert_not_awaited()
    args, kwargs = notifier.send_message.await_args
    assert kwargs["channel_id"] == "C0BJ0PUURRR"
    assert kwargs["blocks"] == hourly_alert_blocks(args[0])
    assert kwargs["blocks"][0]["type"] == "section"


async def test_claimed_alert_falls_back_to_founder_with_blocks_when_unrouted(engine, monkeypatch):
    await _publication(engine, 156, "ad-1")
    monkeypatch.setattr(
        "peermarket_agent.agent.loops.hourly.get_meta_ad_statuses", AsyncMock(return_value=ACTIVE)
    )
    monkeypatch.setattr(
        "peermarket_agent.agent.loops.hourly.fetch_meta_insights",
        AsyncMock(return_value=_snapshot("ad-1", impressions=0)),
    )
    notifier = AsyncMock()
    notifier.notify_founder.return_value = True

    await collect_meta_performance(engine, _settings(), AsyncMock(), notifier, now=NOW)

    notifier.notify_founder.assert_awaited_once()
    notifier.send_message.assert_not_awaited()
    args, kwargs = notifier.notify_founder.await_args
    assert kwargs["blocks"] == hourly_alert_blocks(args[0])


async def test_feature_flags_default_false_and_disabled_pulse_does_not_collect(engine, monkeypatch):
    assert Settings.model_fields["meta_insights_enabled"].default is False
    assert Settings.model_fields["peermarket_attribution_enabled"].default is False
    collector = AsyncMock()
    monkeypatch.setattr("peermarket_agent.agent.loops.hourly.collect_meta_performance", collector)
    peermarket = AsyncMock()
    peermarket.fetch_kpis.return_value = {}

    await run_hourly_pulse(engine, peermarket, settings=_settings(meta_insights_enabled=False))

    collector.assert_not_awaited()


async def test_hourly_collects_before_autonomy_and_passes_explicit_dependencies(
    engine, monkeypatch
):
    calls = []
    collector = AsyncMock(side_effect=lambda *args, **kwargs: calls.append("collect"))
    autonomy = AsyncMock(side_effect=lambda *args, **kwargs: calls.append("autonomy"))
    monkeypatch.setattr("peermarket_agent.agent.loops.hourly.collect_meta_performance", collector)
    monkeypatch.setattr("peermarket_agent.agent.loops.hourly.run_autonomy_cycle", autonomy)
    settings = _settings(meta_insights_enabled=True, meta_autonomy_enabled=True)
    claude, notifier = object(), object()
    peermarket = AsyncMock()
    peermarket.fetch_kpis.return_value = {}

    await run_hourly_pulse(engine, peermarket, settings=settings, notifier=notifier, claude=claude)

    assert calls == ["collect", "autonomy"]
    autonomy.assert_awaited_once_with(engine, claude, notifier, settings)


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

    async def delivered(message, **_kwargs):
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

    async def delivered(message, **_kwargs):
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
