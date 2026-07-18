import asyncio
import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
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
    assert observation.metrics["first_party_landing_to_registration"] is None
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
                    {"event_type": "registration_completed", "event_count": 8},
                    {"event_type": "first_listing_created", "event_count": 3},
                ],
            },
        }
    )
    assert observation.metrics["registrations"] == 8
    assert observation.metrics["first_party_landing_to_registration"] is None


def _complete_performance():
    return {
        "approved_budget_cents": 5_000,
        "delivery": {"condition": "healthy"},
        "meta": {
            "latest": {
                "spend_cents": 2_000,
                "impressions": 1_000,
                "clicks": 100,
                "inline_link_clicks": 80,
                "landing_page_views": 40,
            }
        },
        "attribution": {
            "available": True,
            "events": [
                {"event_type": "landing_view", "event_count": 50},
                {"event_type": "registration_completed", "event_count": 10},
                {"event_type": "first_listing_created", "event_count": 5},
                {"event_type": "first_listing_published", "event_count": 4},
                {"event_type": "identity_verification_completed", "event_count": 2},
            ],
        },
    }


def test_evaluator_produces_exact_design_metric_set():
    metrics = evaluate_publication(_complete_performance()).metrics

    assert metrics == {
        "approved_budget_cents": 5_000,
        "spend_cents": 2_000,
        "delivery_state": "healthy",
        "impressions": 1_000,
        "clicks": 100,
        "link_clicks": 80,
        "meta_landing_page_views": 40,
        "first_party_landing_views": 50,
        "registrations": 10,
        "first_listing_created": 5,
        "first_listing_published": 4,
        "identity_verifications": 2,
        "cost_per_link_click_cents": Decimal("25"),
        "click_to_meta_landing": Decimal("0.5"),
        "first_party_landing_to_registration": Decimal("0.2"),
        "cost_per_registration_cents": Decimal("200"),
        "registration_to_first_listing": Decimal("0.5"),
        "cost_per_first_published_listing_cents": Decimal("500"),
        "identity_verification_conversion": Decimal("0.2"),
    }


@pytest.mark.parametrize(
    ("numerator_field", "denominator_field", "ratio_field"),
    [
        ("spend_cents", "inline_link_clicks", "cost_per_link_click_cents"),
        ("landing_page_views", "inline_link_clicks", "click_to_meta_landing"),
        ("registration_completed", "landing_view", "first_party_landing_to_registration"),
        ("spend_cents", "registration_completed", "cost_per_registration_cents"),
        ("first_listing_created", "registration_completed", "registration_to_first_listing"),
        (
            "spend_cents",
            "first_listing_published",
            "cost_per_first_published_listing_cents",
        ),
        (
            "identity_verification_completed",
            "registration_completed",
            "identity_verification_conversion",
        ),
    ],
)
@pytest.mark.parametrize("denominator", [None, 0])
def test_every_derived_metric_guards_missing_and_zero_denominator(
    numerator_field, denominator_field, ratio_field, denominator
):
    performance = _complete_performance()
    latest = performance["meta"]["latest"]
    events = performance["attribution"]["events"]
    if denominator_field in latest:
        if denominator is None:
            latest.pop(denominator_field)
        else:
            latest[denominator_field] = denominator
    else:
        events[:] = [event for event in events if event["event_type"] != denominator_field]
        if denominator is not None:
            events.append({"event_type": denominator_field, "event_count": denominator})

    assert evaluate_publication(performance).metrics[ratio_field] is None


def test_missing_attribution_events_are_unavailable_not_zero():
    metrics = evaluate_publication(
        {"meta": {"latest": {}}, "attribution": {"available": True, "events": []}}
    ).metrics
    for name in (
        "first_party_landing_views",
        "registrations",
        "first_listing_created",
        "first_listing_published",
        "identity_verifications",
    ):
        assert metrics[name] is None


async def _insert_publication(
    engine,
    *,
    draft_id,
    audience,
    performance,
    ads_url,
    objective="OUTCOME_TRAFFIC",
):
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
                "metadata": json.dumps(
                    {
                        **({"audience_profile_key": audience} if audience is not None else {}),
                        **({"objective": objective} if objective is not None else {}),
                    }
                ),
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
                "performance": json.dumps(performance),
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
                    "window_definition": "rolling-2-calendar-days",
                }
            },
            "attribution": {"available": False, "error": "password=super-secret"},
        },
    )
    notifier = AsyncMock()
    now = datetime(2026, 7, 16, 9, tzinfo=UTC)

    assert await run_daily_performance(database_engine, notifier, object(), now=now) == 1
    assert await run_daily_performance(database_engine, notifier, object(), now=now) == 0

    async with database_engine.connect() as conn:
        performance = (
            await conn.execute(text("SELECT performance FROM publications WHERE draft_id=501"))
        ).scalar_one()
    assert len(performance["daily_observations"]) == 1
    message = notifier.send_message.await_args_list[0].args[0]
    assert "unavailable" in message
    assert "account dates 2026-07-15 → 2026-07-16 (Europe/Brussels)" in message
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
                "window_definition": "rolling-2-calendar-days",
            }
        },
        "attribution": {
            "available": True,
            "events": [{"event_type": "registration_completed", "event_count": 10}],
        },
    }
    await _insert_publication(
        database_engine, draft_id=601, audience="declutterers", performance=base, ads_url=None
    )
    base["meta"]["latest"]["landing_page_views"] = 31
    base["attribution"]["events"][0]["event_count"] = 11
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
    assert len(rows) == 2
    assert rows[0]["seen_n_times"] == 1
    evidence = rows[0]["evidence_links"]
    assert evidence["window"] == {
        "start": "2026-07-15",
        "stop": "2026-07-16",
        "definition": "rolling-2-calendar-days",
        "inclusive_days": 2,
    }
    assert evidence["decision"]["eligible"] is True
    assert evidence["decision"]["learning_type"] in {"delivery", "conversion"}
    assert evidence["decision"]["reason"].endswith("_thresholds_met")
    assert evidence["decision"]["metric"]
    assert evidence["decision"]["outcome"]["winner_publication_id"] in {1, 2}
    assert {
        "channel": "meta",
        "objective": "OUTCOME_TRAFFIC",
        "language": "NL",
        "audience": "declutterers",
        "window_definition": "rolling-2-calendar-days",
    }.items() <= evidence["dimensions"].items()
    assert evidence["dimensions"]["account_timezone"] == "Europe/Brussels"
    assert evidence["dimensions"]["utc_start"]
    assert evidence["dimensions"]["utc_stop_exclusive"]
    assert evidence["thresholds"] == {
        "impressions": 1_000,
        "landing_page_views": 30,
        "registrations": 10,
    }
    assert evidence["sample"]["variants"] == 2
    async with database_engine.connect() as conn:
        observations = (
            (
                await conn.execute(
                    text(
                        "SELECT id, performance->'daily_observations'->0 AS observation FROM publications ORDER BY id"
                    )
                )
            )
            .mappings()
            .all()
        )
    assert [variant["publication_id"] for variant in evidence["variants"]] == [1, 2]
    for persisted_variant, source in zip(evidence["variants"], observations, strict=True):
        assert persisted_variant["evidence_id"] == source["observation"]["evidence_id"]
        assert persisted_variant["compared_values"] == source["observation"]["metrics"]
        assert persisted_variant["sample_sizes"] == {
            "impressions": source["observation"]["metrics"]["impressions"],
            "meta_landing_page_views": source["observation"]["metrics"]["meta_landing_page_views"],
            "registrations": source["observation"]["metrics"]["registrations"],
        }


async def test_equal_metrics_never_persist_directional_learning(database_engine):
    await run_migrations(database_engine)
    await seed(database_engine)
    base = _complete_performance()
    base["meta"]["latest"].update(
        window_start="2026-07-14",
        window_stop="2026-07-15",
        window_definition="rolling-2-inclusive-calendar-days",
    )
    for draft_id in (611, 612):
        await _insert_publication(
            database_engine,
            draft_id=draft_id,
            audience="declutterers",
            performance=base,
            ads_url=None,
        )

    await run_daily_performance(
        database_engine, AsyncMock(), object(), now=datetime(2026, 7, 16, 9, tzinfo=UTC)
    )

    async with database_engine.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM learnings"))).scalar_one() == 0


async def test_summary_labels_account_dates_and_explicit_utc_interval(database_engine):
    await _prepared_summary_publication(database_engine)
    notifier = AsyncMock()

    await run_daily_performance(
        database_engine, notifier, object(), now=datetime(2026, 7, 17, 9, tzinfo=UTC)
    )

    message = notifier.send_message.await_args.args[0]
    assert "account dates 2026-07-14 → 2026-07-16 (Europe/Brussels)" in message
    assert "UTC interval 2026-07-13T22:00:00+00:00 → 2026-07-16T22:00:00+00:00" in message
    assert "2026-07-14 → 2026-07-16 UTC" not in message


async def test_daily_slack_contains_complete_design_metrics_and_samples(database_engine):
    await run_migrations(database_engine)
    await seed(database_engine)
    performance = _complete_performance()
    performance["meta"]["latest"].update(
        window_start="2026-07-14",
        window_stop="2026-07-16",
        window_definition="rolling-3-inclusive-calendar-days",
    )
    await _insert_publication(
        database_engine,
        draft_id=650,
        audience="declutterers",
        performance=performance,
        ads_url="https://business.facebook.com/adsmanager/manage/ads?act=123",
    )
    notifier = AsyncMock()

    await run_daily_performance(
        database_engine, notifier, object(), now=datetime(2026, 7, 16, 9, tzinfo=UTC)
    )

    message = notifier.send_message.await_args.args[0]
    for fragment in (
        "approved budget",
        "spend 2000 cents",
        "delivery healthy",
        "impressions 1000",
        "clicks 100",
        "link clicks 80",
        "Meta LPV 40",
        "first-party landings 50",
        "attributed registrations 10",
        "first listings created 5",
        "first listings published 4",
        "identity verifications 2",
        "cost/link click 25",
        "click→Meta LPV 0.5",
        "first-party landing→registration 0.2",
        "cost/registration 200",
        "registration→first listing 0.5",
        "cost/first published listing 500",
        "identity verification conversion 0.2",
        "sample sizes: impressions 1000, Meta LPV 40, registrations 10",
        "Ads Manager:",
    ):
        assert fragment in message
    assert "caused" not in message.lower()


@pytest.mark.parametrize(
    "latest",
    [
        {"window_start": "2026-07-15", "window_stop": "2026-07-16"},
        {
            "window_start": "not-a-date",
            "window_stop": "2026-07-16",
            "window_definition": "rolling-3-calendar-days",
        },
        {
            "window_start": "2026-07-16",
            "window_stop": "2026-07-15",
            "window_definition": "rolling-3-calendar-days",
        },
    ],
)
async def test_missing_or_invalid_source_window_creates_no_completed_observation(
    database_engine, latest
):
    await run_migrations(database_engine)
    await seed(database_engine)
    await _insert_publication(
        database_engine,
        draft_id=701,
        audience="declutterers",
        ads_url=None,
        performance={"meta": {"latest": latest}, "attribution": {"available": False}},
    )
    notifier = AsyncMock()

    assert (
        await run_daily_performance(
            database_engine, notifier, object(), now=datetime(2026, 7, 16, 9, tzinfo=UTC)
        )
        == 0
    )
    async with database_engine.connect() as conn:
        performance = (
            await conn.execute(text("SELECT performance FROM publications WHERE draft_id=701"))
        ).scalar_one()
    assert "daily_observations" not in performance
    notifier.send_message.assert_awaited_once()
    assert (
        notifier.send_message.await_args.args[0] == "Publication #1 — source window unavailable"
    )
    rows = await _summary_outbox(database_engine)
    assert len(rows) == 1
    assert rows[0]["summary_kind"] == "source_window_unavailable"
    assert rows[0]["run_day"].isoformat() == "2026-07-16"
    assert rows[0]["window_start"] is None
    assert rows[0]["window_stop"] is None
    assert rows[0]["window_definition"] is None
    assert rows[0]["evidence_ids"] == []
    async with database_engine.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM learnings"))).scalar_one() == 0


async def test_explicit_one_day_inclusive_source_window_is_valid(database_engine):
    await run_migrations(database_engine)
    await seed(database_engine)
    await _insert_publication(
        database_engine,
        draft_id=702,
        audience="declutterers",
        ads_url=None,
        performance={
            "meta": {
                "latest": {
                    "window_start": "2026-07-16",
                    "window_stop": "2026-07-16",
                    "window_definition": "rolling-1-inclusive-calendar-day",
                }
            },
            "attribution": {"available": False},
        },
    )

    assert (
        await run_daily_performance(
            database_engine,
            AsyncMock(),
            object(),
            now=datetime(2026, 7, 16, 9, tzinfo=UTC),
        )
        == 1
    )


async def test_missing_objective_or_audience_never_creates_learning(database_engine):
    await run_migrations(database_engine)
    await seed(database_engine)
    base = _complete_performance()
    base["meta"]["latest"].update(
        window_start="2026-07-14",
        window_stop="2026-07-16",
        window_definition="rolling-3-calendar-days",
    )
    for draft_id in (801, 802):
        await _insert_publication(
            database_engine,
            draft_id=draft_id,
            audience=None,
            objective=None,
            performance=base,
            ads_url=None,
        )
    await run_daily_performance(
        database_engine, AsyncMock(), object(), now=datetime(2026, 7, 16, 9, tzinfo=UTC)
    )
    async with database_engine.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM learnings"))).scalar_one() == 0


async def test_concurrent_daily_replay_inserts_one_observation_and_learning(database_engine):
    await run_migrations(database_engine)
    await seed(database_engine)
    base = _complete_performance()
    base["meta"]["latest"].update(
        window_start="2026-07-14",
        window_stop="2026-07-16",
        window_definition="rolling-3-calendar-days",
    )
    for draft_id in (901, 902):
        variant_performance = json.loads(json.dumps(base))
        if draft_id == 902:
            variant_performance["meta"]["latest"]["landing_page_views"] += 1
            for event in variant_performance["attribution"]["events"]:
                if event["event_type"] == "registration_completed":
                    event["event_count"] += 1
        await _insert_publication(
            database_engine,
            draft_id=draft_id,
            audience="declutterers",
            performance=variant_performance,
            ads_url=None,
        )
    now = datetime(2026, 7, 16, 9, tzinfo=UTC)

    results = await asyncio.gather(
        run_daily_performance(database_engine, AsyncMock(), object(), now=now),
        run_daily_performance(database_engine, AsyncMock(), object(), now=now),
    )

    assert sorted(results) == [0, 2]
    async with database_engine.connect() as conn:
        observations = (
            await conn.execute(
                text(
                    "SELECT sum(jsonb_array_length(performance->'daily_observations')) FROM publications"
                )
            )
        ).scalar_one()
        learning_count = (await conn.execute(text("SELECT count(*) FROM learnings"))).scalar_one()
    assert observations == 2
    assert learning_count == 2


async def test_new_window_reinforces_once_and_retains_replayable_prior_evidence(database_engine):
    await run_migrations(database_engine)
    await seed(database_engine)
    base = _complete_performance()
    base["meta"]["latest"].update(
        window_start="2026-07-14",
        window_stop="2026-07-16",
        window_definition="rolling-3-calendar-days",
    )
    for draft_id in (1001, 1002):
        variant_performance = json.loads(json.dumps(base))
        if draft_id == 1002:
            variant_performance["meta"]["latest"]["landing_page_views"] += 1
            for event in variant_performance["attribution"]["events"]:
                if event["event_type"] == "registration_completed":
                    event["event_count"] += 1
        await _insert_publication(
            database_engine,
            draft_id=draft_id,
            audience="declutterers",
            performance=variant_performance,
            ads_url=None,
        )
    await run_daily_performance(
        database_engine, AsyncMock(), object(), now=datetime(2026, 7, 16, 9, tzinfo=UTC)
    )
    async with database_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE publications SET performance=jsonb_set(jsonb_set(performance, "
                "'{meta,latest,window_start}', '\"2026-07-15\"'), "
                "'{meta,latest,window_stop}', '\"2026-07-17\"')"
            )
        )

    await run_daily_performance(
        database_engine, AsyncMock(), object(), now=datetime(2026, 7, 17, 9, tzinfo=UTC)
    )
    await run_daily_performance(
        database_engine, AsyncMock(), object(), now=datetime(2026, 7, 17, 9, tzinfo=UTC)
    )

    async with database_engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT evidence_links, seen_n_times FROM learnings "
                        "WHERE scope LIKE 'conversion:%'"
                    )
                )
            )
            .mappings()
            .one()
        )
    assert row["seen_n_times"] == 2
    assert len(row["evidence_links"]["runs"]) == 2
    assert [run["window"]["stop"] for run in row["evidence_links"]["runs"]] == [
        "2026-07-16",
        "2026-07-17",
    ]


async def _summary_outbox(database_engine):
    async with database_engine.connect() as conn:
        return [
            dict(row)
            for row in (
                await conn.execute(
                    text(
                        "SELECT summary_key, summary_kind, run_day, window_start, window_stop, "
                        "window_definition, "
                        "publication_ids, evidence_ids, message, status, attempt_count, "
                        "last_attempt_at, sent_at, claim_token, claim_expires_at, last_failure "
                        "FROM daily_performance_summary_outbox ORDER BY window_start, id"
                    )
                )
            )
            .mappings()
            .all()
        ]


async def _prepared_summary_publication(database_engine, draft_id=1101):
    await run_migrations(database_engine)
    await seed(database_engine)
    performance = _complete_performance()
    performance["meta"]["latest"].update(
        window_start="2026-07-14",
        window_stop="2026-07-16",
        window_definition="rolling-3-inclusive-calendar-days",
    )
    await _insert_publication(
        database_engine,
        draft_id=draft_id,
        audience="declutterers",
        performance=performance,
        ads_url="https://business.facebook.com/adsmanager/manage/ads?act=123",
    )


async def test_false_delivery_stays_pending_after_persisting_sanitized_summary(database_engine):
    await _prepared_summary_publication(database_engine)
    notifier = AsyncMock()
    notifier.send_message.side_effect = RuntimeError("slack unavailable")

    await run_daily_performance(
        database_engine, notifier, object(), now=datetime(2026, 7, 16, 9, tzinfo=UTC)
    )

    row = (await _summary_outbox(database_engine))[0]
    assert row["status"] == "pending"
    assert row["attempt_count"] == 1
    assert row["last_attempt_at"] is not None
    assert row["sent_at"] is None
    assert row["claim_token"] is None
    assert row["claim_expires_at"] is None
    assert row["last_failure"] == "notification_exception"
    assert row["publication_ids"] == [1]
    assert row["evidence_ids"][0].startswith(
        "publication:1:2026-07-14:2026-07-16:rolling-3-inclusive-calendar-days:Europe/Brussels:"
    )
    assert "secret" not in row["message"].lower()


async def test_unavailable_window_false_delivery_retries_same_diagnostic(database_engine):
    await run_migrations(database_engine)
    await seed(database_engine)
    await _insert_publication(
        database_engine,
        draft_id=1201,
        audience="declutterers",
        ads_url=None,
        performance={"meta": {"latest": {}}, "attribution": {"available": False}},
    )
    now = datetime(2026, 7, 16, 9, tzinfo=UTC)
    failed = AsyncMock()
    failed.send_message.side_effect = RuntimeError("slack unavailable")
    await run_daily_performance(database_engine, failed, object(), now=now)
    pending = (await _summary_outbox(database_engine))[0]
    assert pending["status"] == "pending"
    assert pending["attempt_count"] == 1
    notifier = AsyncMock()

    await run_daily_performance(database_engine, notifier, object(), now=now)

    notifier.send_message.assert_awaited_once()
    assert (
        notifier.send_message.await_args.args[0] == "Publication #1 — source window unavailable"
    )
    rows = await _summary_outbox(database_engine)
    assert len(rows) == 1
    assert rows[0]["status"] == "sent"
    assert rows[0]["attempt_count"] == 2


async def test_unavailable_window_same_day_idempotent_and_next_day_reports_again(database_engine):
    await run_migrations(database_engine)
    await seed(database_engine)
    await _insert_publication(
        database_engine,
        draft_id=1301,
        audience="declutterers",
        ads_url=None,
        performance={"meta": {"latest": {}}, "attribution": {"available": False}},
    )
    notifier = AsyncMock()
    day_one = datetime(2026, 7, 16, 9, tzinfo=UTC)

    await run_daily_performance(database_engine, notifier, object(), now=day_one)
    await run_daily_performance(database_engine, notifier, object(), now=day_one)

    assert notifier.send_message.await_count == 1
    assert len(await _summary_outbox(database_engine)) == 1
    await run_daily_performance(
        database_engine,
        notifier,
        object(),
        now=datetime(2026, 7, 17, 9, tzinfo=UTC),
    )
    assert notifier.send_message.await_count == 2
    rows = await _summary_outbox(database_engine)
    assert [row["run_day"].isoformat() for row in rows] == ["2026-07-16", "2026-07-17"]
    assert {row["summary_key"] for row in rows} == {
        "daily-performance-unavailable:1:2026-07-16",
        "daily-performance-unavailable:1:2026-07-17",
    }
    async with database_engine.connect() as conn:
        performance = (
            await conn.execute(text("SELECT performance FROM publications WHERE id=1"))
        ).scalar_one()
        learning_count = (await conn.execute(text("SELECT count(*) FROM learnings"))).scalar_one()
    assert "daily_observations" not in performance
    assert learning_count == 0


async def test_delivery_exception_stays_pending_without_persisting_exception_text(database_engine):
    await _prepared_summary_publication(database_engine)
    notifier = AsyncMock()
    notifier.send_message.side_effect = RuntimeError("password=super-secret")

    await run_daily_performance(
        database_engine, notifier, object(), now=datetime(2026, 7, 16, 9, tzinfo=UTC)
    )

    row = (await _summary_outbox(database_engine))[0]
    assert row["status"] == "pending"
    assert row["last_failure"] == "notification_exception"
    assert "super-secret" not in str(row)


async def test_next_day_retries_old_pending_before_sending_new_summary(database_engine):
    await _prepared_summary_publication(database_engine)
    first_notifier = AsyncMock()
    first_notifier.send_message.side_effect = RuntimeError("slack unavailable")
    await run_daily_performance(
        database_engine, first_notifier, object(), now=datetime(2026, 7, 16, 9, tzinfo=UTC)
    )
    async with database_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE publications SET performance=jsonb_set(jsonb_set(performance, "
                "'{meta,latest,window_start}', '\"2026-07-15\"'), "
                "'{meta,latest,window_stop}', '\"2026-07-17\"')"
            )
        )
    notifier = AsyncMock()

    await run_daily_performance(
        database_engine, notifier, object(), now=datetime(2026, 7, 17, 9, tzinfo=UTC)
    )

    assert notifier.send_message.await_count == 2
    old_message, new_message = [item.args[0] for item in notifier.send_message.await_args_list]
    assert "2026-07-14 → 2026-07-16" in old_message
    assert "2026-07-15 → 2026-07-17" in new_message
    rows = await _summary_outbox(database_engine)
    assert [row["status"] for row in rows] == ["sent", "sent"]
    assert [row["attempt_count"] for row in rows] == [2, 1]


async def test_concurrent_runs_claim_one_summary_sender(database_engine):
    await _prepared_summary_publication(database_engine)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def deliver(_message, **_kwargs):
        entered.set()
        await release.wait()
        return True

    notifier = AsyncMock()
    notifier.send_message.side_effect = deliver
    first = asyncio.create_task(
        run_daily_performance(
            database_engine, notifier, object(), now=datetime(2026, 7, 16, 9, tzinfo=UTC)
        )
    )
    await entered.wait()
    second = asyncio.create_task(
        run_daily_performance(
            database_engine, notifier, object(), now=datetime(2026, 7, 16, 9, tzinfo=UTC)
        )
    )
    await asyncio.sleep(0.05)
    release.set()
    await asyncio.gather(first, second)

    notifier.send_message.assert_awaited_once()
    row = (await _summary_outbox(database_engine))[0]
    assert row["status"] == "sent"
    assert row["attempt_count"] == 1


async def test_stale_claim_is_retried(database_engine):
    await _prepared_summary_publication(database_engine)
    first = AsyncMock()
    first.send_message.side_effect = RuntimeError("slack unavailable")
    now = datetime(2026, 7, 16, 9, tzinfo=UTC)
    await run_daily_performance(database_engine, first, object(), now=now)
    async with database_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE daily_performance_summary_outbox SET claim_token='abandoned', "
                "claim_expires_at=:expired WHERE status='pending'"
            ),
            {"expired": datetime(2026, 7, 16, 8, tzinfo=UTC)},
        )
    notifier = AsyncMock()

    await run_daily_performance(database_engine, notifier, object(), now=now)

    notifier.send_message.assert_awaited_once()
    row = (await _summary_outbox(database_engine))[0]
    assert row["status"] == "sent"
    assert row["attempt_count"] == 2


async def test_successful_summary_is_idempotent_across_daily_replay(database_engine):
    await _prepared_summary_publication(database_engine)
    notifier = AsyncMock()
    now = datetime(2026, 7, 16, 9, tzinfo=UTC)

    await run_daily_performance(database_engine, notifier, object(), now=now)
    await run_daily_performance(database_engine, notifier, object(), now=now)

    notifier.send_message.assert_awaited_once()
    assert notifier.send_message.await_count == 1
    rows = await _summary_outbox(database_engine)
    assert len(rows) == 1
    assert rows[0]["status"] == "sent"
    assert rows[0]["attempt_count"] == 1


async def test_existing_immutable_observation_without_outbox_is_recovered(database_engine):
    await _prepared_summary_publication(database_engine)
    failed = AsyncMock()
    failed.send_message.side_effect = RuntimeError("slack unavailable")
    now = datetime(2026, 7, 16, 9, tzinfo=UTC)
    await run_daily_performance(database_engine, failed, object(), now=now)
    async with database_engine.begin() as conn:
        await conn.execute(text("DELETE FROM daily_performance_summary_outbox"))
    notifier = AsyncMock()

    assert await run_daily_performance(database_engine, notifier, object(), now=now) == 0

    notifier.send_message.assert_awaited_once()
    row = (await _summary_outbox(database_engine))[0]
    assert row["status"] == "sent"
    assert row["evidence_ids"][0].startswith(
        "publication:1:2026-07-14:2026-07-16:rolling-3-inclusive-calendar-days:Europe/Brussels:"
    )


async def test_daily_summary_routes_to_report_channel_with_header_blocks(database_engine):
    await _prepared_summary_publication(database_engine)
    notifier = AsyncMock()
    settings = SimpleNamespace(slack_report_channel_meta="C0BJ0PUURRR")

    await run_daily_performance(
        database_engine, notifier, settings, now=datetime(2026, 7, 16, 9, tzinfo=UTC)
    )

    notifier.send_message.assert_awaited_once()
    notifier.notify_founder.assert_not_awaited()
    args, kwargs = notifier.send_message.await_args
    assert kwargs["channel_id"] == "C0BJ0PUURRR"
    assert kwargs["blocks"][0]["type"] == "header"
    assert args[0].splitlines()[0] == kwargs["blocks"][0]["text"]["text"].removeprefix("📊 ")
    row = (await _summary_outbox(database_engine))[0]
    assert row["status"] == "sent"


async def test_daily_summary_falls_back_to_founder_channel_when_unrouted(database_engine):
    await _prepared_summary_publication(database_engine)
    notifier = AsyncMock()
    settings = SimpleNamespace(slack_report_channel_meta=None)

    await run_daily_performance(
        database_engine, notifier, settings, now=datetime(2026, 7, 16, 9, tzinfo=UTC)
    )

    notifier.send_message.assert_awaited_once()
    notifier.notify_founder.assert_not_awaited()
    _, kwargs = notifier.send_message.await_args
    assert kwargs["channel_id"] is None
    assert kwargs["blocks"][0]["type"] == "header"
    row = (await _summary_outbox(database_engine))[0]
    assert row["status"] == "sent"
