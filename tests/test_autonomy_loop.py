"""Task 7 autonomous lifecycle orchestration contracts."""

import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from peermarket_agent.agent.loops.autonomy import (
    _audit,
    _persisted_hook_variants,
    persist_autonomy_inputs,
    prepare_hook_experiment,
    run_autonomy_cycle,
)
from peermarket_agent.autonomy.contracts import DecisionKind, FrozenDecision
from peermarket_agent.autonomy.hook_experiments import build_hook_experiment
from peermarket_agent.autonomy.store import record_experiment
from peermarket_agent.db.migrations import run_migrations

NOW = datetime(2026, 7, 17, 12, tzinfo=UTC)


def _hook_draft():
    return {
        "id": 156,
        "campaign_id": "120249125021520342",
        "ad_set_id": "120249125021520343",
        "landing_page_url": "https://peermarket.eu/signup",
        "fixed_identity": {
            "audience": "declutterers",
            "optimization": "LANDING_PAGE_VIEWS",
            "format": "single_image",
            "visual": "asset-1",
            "delivery": "lowest_cost",
        },
        "language_bundles": {
            locale: {
                "hook": "baseline",
                "body": f"{locale} body",
                "headline": f"{locale} headline",
                "description": f"{locale} description",
                "cta_label": "Learn More",
            }
            for locale in ("NL", "FR", "EN")
        },
    }


@pytest.mark.asyncio
async def test_prepare_hook_experiment_persists_exactly_three_without_meta_mutation(monkeypatch):
    draft = _hook_draft()
    expected = build_hook_experiment(draft, "warm and practical", "shadow-1")
    persisted = AsyncMock()
    enqueue = AsyncMock()
    meta_mutation = AsyncMock()
    monkeypatch.setattr("peermarket_agent.agent.loops.autonomy.record_experiment", persisted)
    monkeypatch.setattr("peermarket_agent.agent.loops.autonomy.enqueue_action", enqueue)
    monkeypatch.setattr(
        "peermarket_agent.agent.loops.autonomy.execute_production_claim", meta_mutation
    )
    settings = SimpleNamespace(
        meta_autonomy_shadow=True,
        meta_autonomy_campaign_ids=(draft["campaign_id"],),
        meta_autonomy_variant_count=3,
        meta_autonomy_experiment_id=expected.experiment_id,
    )
    engine = object()
    result = await prepare_hook_experiment(
        engine, settings, draft, "warm and practical", "shadow-1"
    )
    assert result == expected
    assert len(result.variants) == 3
    persisted.assert_awaited_once_with(engine, result)
    enqueue.assert_not_awaited()
    meta_mutation.assert_not_awaited()


@pytest.mark.asyncio
async def test_prepare_hook_experiment_rejects_non_shadow_or_wrong_identity(monkeypatch):
    persisted = AsyncMock()
    monkeypatch.setattr("peermarket_agent.agent.loops.autonomy.record_experiment", persisted)
    settings = SimpleNamespace(
        meta_autonomy_shadow=False,
        meta_autonomy_campaign_ids=("120249125021520342",),
        meta_autonomy_variant_count=3,
        meta_autonomy_experiment_id="",
    )
    with pytest.raises(ValueError, match="shadow-only"):
        await prepare_hook_experiment(object(), settings, _hook_draft(), "voice", 1)
    persisted.assert_not_awaited()


@pytest.mark.asyncio
async def test_persisted_nine_locale_rows_feed_three_logical_policy_variants(engine):
    experiment = build_hook_experiment(_hook_draft(), "warm", "policy")
    await record_experiment(engine, experiment)
    samples = {
        variant.variant_id: {
            locale: {"impressions": 400, "landing_page_views": 20, "registrations": number}
            for locale in ("NL", "FR", "EN")
        }
        for number, variant in enumerate(experiment.variants, 1)
    }
    variants = await _persisted_hook_variants(
        engine,
        experiment.experiment_id,
        {"publication_id": 7, "external_ids": {"campaign_id": experiment.campaign_id}},
        {"hook_experiment_variants": samples},
    )
    assert [item["variant_id"] for item in variants] == [
        f"{experiment.experiment_id}:{number:02}" for number in (1, 2, 3)
    ]
    assert [item["impressions"] for item in variants] == [1200, 1200, 1200]
    assert [item["registrations"] for item in variants] == [3, 6, 9]
    assert all(item["creative_dimension"] == "hook" for item in variants)

    samples[experiment.variants[0].variant_id]["NL"]["captured_at"] = "drifted"
    assert (
        await _persisted_hook_variants(
            engine,
            experiment.experiment_id,
            {
                "publication_id": 7,
                "external_ids": {"campaign_id": experiment.campaign_id},
            },
            {
                "autonomy_basis": {
                    "captured_at": NOW.isoformat(),
                    "window_start": (NOW - timedelta(days=1)).isoformat(),
                    "window_end": NOW.isoformat(),
                },
                "hook_experiment_variants": samples,
            },
        )
        is None
    )


@pytest.mark.asyncio
async def test_real_candidate_cycle_uses_collected_hook_metrics_for_neutral_and_qualified(engine):
    experiment = build_hook_experiment(_hook_draft(), "warm", "cycle")
    await record_experiment(engine, experiment)
    metrics = {
        variant.variant_id: {
            locale: {
                "ad_id": f"ad-{number}-{locale}",
                "impressions": 400,
                "landing_page_views": 20,
                "registrations": number * 4,
                "captured_at": NOW.isoformat(),
                "window_start": (NOW - timedelta(days=1)).isoformat(),
                "window_stop": NOW.isoformat(),
            }
            for locale in ("NL", "FR", "EN")
        }
        for number, variant in enumerate(experiment.variants, 1)
    }
    ids = {"campaign_id": experiment.campaign_id, "ad_set_id": "20", "ad_id": "30"}
    basis = {
        "campaign_id": experiment.campaign_id,
        "external_ids": ids,
        "approved_budget_cents": 1000,
        "captured_at": NOW.isoformat(),
        "window_start": (NOW - timedelta(days=1)).isoformat(),
        "window_end": NOW.isoformat(),
        "delivery_state": "healthy",
        "attribution_complete": True,
        "complete": True,
    }
    async with engine.begin() as conn:
        action_type = await conn.scalar(
            text(
                "INSERT INTO action_types(name,risk_tier,default_autonomy) VALUES ('hook-cycle','high','propose') RETURNING id"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO drafts(id,action_type_id,channel,language,status,metadata) VALUES (156,:type,'meta','MULTI','published','{}')"
            ),
            {"type": action_type},
        )
        await conn.execute(
            text(
                "INSERT INTO publications(draft_id,channel,state,external_ids,approved_budget_cents,performance) VALUES (156,'meta','active',CAST(:ids AS JSONB),1000,CAST(:performance AS JSONB))"
            ),
            {
                "ids": json.dumps(ids),
                "performance": json.dumps(
                    {"autonomy_basis": basis, "hook_experiment_variants": metrics}
                ),
            },
        )
        await conn.execute(
            text(
                "INSERT INTO drafts(id,action_type_id,channel,language,status,metadata) VALUES (157,:type,'meta','MULTI','approved','{}')"
            ),
            {"type": action_type},
        )
        decision_id = await conn.scalar(
            text(
                "INSERT INTO autonomous_decisions(decision_key,kind,campaign_id,window_start,window_end,evidence,reason) VALUES ('cycle-progress','replace',:campaign,:start,:stop,'{}','test') RETURNING id"
            ),
            {
                "campaign": experiment.campaign_id,
                "start": NOW - timedelta(days=1),
                "stop": NOW,
            },
        )
        action_id = await conn.scalar(
            text(
                "INSERT INTO autonomous_actions(decision_id,campaign_id,status) VALUES (:decision,:campaign,'executing') RETURNING id"
            ),
            {"decision": decision_id, "campaign": experiment.campaign_id},
        )
        progress = {
            f"variant:{experiment.experiment_id}:{number:02}:ad_id:{locale}": f"ad-{number}-{locale}"
            for number in (1, 2, 3)
            for locale in ("NL", "FR", "EN")
        }
        await conn.execute(
            text(
                "INSERT INTO autonomous_replacement_publications(action_id,replacement_draft_id,source_draft_id,state,frozen_budget_cents,source_campaign_id,changed_dimension,landing_page_url,progress) VALUES (:action,157,156,'paused',1000,:campaign,'hook','https://peermarket.eu/signup',CAST(:progress AS JSONB))"
            ),
            {
                "action": action_id,
                "campaign": experiment.campaign_id,
                "progress": json.dumps(progress),
            },
        )
    settings = _limits(
        meta_autonomy_campaign_ids=(experiment.campaign_id,),
        meta_autonomy_experiment_id=experiment.experiment_id,
        learning_min_impressions=1000,
        learning_min_landing_page_views=30,
        learning_min_registrations=10,
    )
    qualified_metrics = json.loads(json.dumps(metrics))
    qualified_metrics[experiment.variants[0].variant_id]["NL"].update(
        {
            "hook": "raw-hook-secret",
            "body": "raw-body-secret",
            "headline": "raw-headline-secret",
            "description": "raw-description-secret",
            "access_token": "token-secret",
        }
    )
    for locale in ("NL", "FR", "EN"):
        metrics[experiment.variants[2].variant_id][locale]["registrations"] = 0
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE publications SET performance=jsonb_set(performance,'{hook_experiment_variants}',CAST(:metrics AS JSONB)) WHERE draft_id=156"
            ),
            {"metrics": json.dumps(metrics)},
        )
    neutral_result = await run_autonomy_cycle(engine, object(), None, settings, now=NOW)
    assert neutral_result == {"evaluated": 1, "queued": 0, "executed": 0, "failed": 0}
    async with engine.connect() as conn:
        neutral = (
            (
                await conn.execute(
                    text("SELECT kind,reason FROM autonomous_decisions ORDER BY id DESC LIMIT 1")
                )
            )
            .mappings()
            .one()
        )
    assert neutral["kind"] == "observe"
    assert neutral["reason"] == "insufficient_evidence"

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE publications SET performance=jsonb_set(performance,'{hook_experiment_variants}',CAST(:metrics AS JSONB)) WHERE draft_id=156"
            ),
            {"metrics": json.dumps(qualified_metrics)},
        )
    qualified_result = await run_autonomy_cycle(
        engine, object(), None, settings, now=NOW + timedelta(seconds=1)
    )
    assert qualified_result == {"evaluated": 1, "queued": 0, "executed": 0, "failed": 0}
    async with engine.connect() as conn:
        qualified = (
            (
                await conn.execute(
                    text("SELECT kind,evidence FROM autonomous_decisions ORDER BY id DESC LIMIT 1")
                )
            )
            .mappings()
            .one()
        )
        audit_payload = await conn.scalar(
            text("SELECT payload FROM slack_outbox WHERE autonomy_campaign_id=:campaign"),
            {"campaign": experiment.campaign_id},
        )
    assert qualified["kind"] != "observe"
    assert qualified["evidence"]["experiment_id"] == experiment.experiment_id
    assert audit_payload["experiment_id"] == experiment.experiment_id
    assert audit_payload["variant_ids"] == [variant.variant_id for variant in experiment.variants]
    assert len(audit_payload["evidence"]) == 3
    assert audit_payload["thresholds"]["min_impressions"] == 1000
    assert audit_payload["evidence_window"]["captured_at"] == NOW.isoformat()
    assert audit_payload["next_evaluation_at"]
    serialized_audit = json.dumps(audit_payload).casefold()
    assert "baseline" not in serialized_audit
    assert all(
        secret not in serialized_audit
        for secret in (
            "raw-hook-secret",
            "raw-body-secret",
            "raw-headline-secret",
            "raw-description-secret",
            "token-secret",
        )
    )


@pytest.fixture
async def engine():
    value = create_async_engine(os.environ["AGENT_DB_URL"], future=True)
    async with value.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await run_migrations(value)
    yield value
    await value.dispose()


def _limits(**overrides):
    values = {
        "meta_autonomy_enabled": True,
        "meta_autonomy_shadow": True,
        "meta_autonomy_campaign_ids": ("10",),
        "performance_snapshot_max_age_hours": 2,
        "learning_min_impressions": 100,
        "learning_min_landing_page_views": 10,
        "learning_min_registrations": 1,
        "meta_autonomy_cooldown_hours": 24,
        "meta_autonomy_max_test_days": 7,
        "meta_autonomy_max_replacements_24h": 1,
        "meta_autonomy_max_increase_percent": 20,
        "meta_autonomy_max_daily_budget_eur": 20,
        "meta_no_delivery_grace_hours": 2,
        "meta_account_timezone": "Europe/Brussels",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_disabled_cycle_is_constant_time_without_database_or_network_work():
    result = await run_autonomy_cycle(
        object(), object(), object(), SimpleNamespace(meta_autonomy_enabled=False), now=NOW
    )
    assert result == {"evaluated": 0, "queued": 0, "executed": 0, "failed": 0}


@pytest.mark.asyncio
async def test_shadow_cycle_persists_every_decision_without_queue_or_execution(monkeypatch):
    decisions = [
        FrozenDecision(
            DecisionKind.SCALE,
            campaign,
            {"snapshot_id": f"snapshot-{campaign}"},
            "winner",
            NOW - timedelta(days=1),
            NOW,
            f"decision-{campaign}",
            1000,
            1200,
        )
        for campaign in ("10", "11")
    ]
    monkeypatch.setattr(
        "peermarket_agent.agent.loops.autonomy._eligible_campaigns",
        AsyncMock(
            return_value=[{"decision": item, "draft_id": i + 1} for i, item in enumerate(decisions)]
        ),
    )
    recorded = AsyncMock()
    queued = AsyncMock()
    executed = AsyncMock()
    audit = AsyncMock()
    monkeypatch.setattr("peermarket_agent.agent.loops.autonomy.record_decision", recorded)
    monkeypatch.setattr("peermarket_agent.agent.loops.autonomy.enqueue_action", queued)
    monkeypatch.setattr("peermarket_agent.agent.loops.autonomy.execute_production_claim", executed)
    monkeypatch.setattr("peermarket_agent.agent.loops.autonomy._audit", audit)
    settings = SimpleNamespace(
        meta_autonomy_enabled=True,
        meta_autonomy_shadow=True,
        meta_autonomy_campaign_ids=("10", "11"),
    )

    result = await run_autonomy_cycle(object(), object(), object(), settings, now=NOW)

    assert result == {"evaluated": 2, "queued": 0, "executed": 0, "failed": 0}
    assert recorded.await_count == 2
    queued.assert_not_awaited()
    executed.assert_not_awaited()
    assert audit.await_count == 2


@pytest.mark.asyncio
async def test_one_campaign_persistence_failure_does_not_block_the_next(monkeypatch):
    decisions = [
        FrozenDecision(
            DecisionKind.OBSERVE,
            campaign,
            {"snapshot_id": campaign},
            "observe",
            NOW - timedelta(days=1),
            NOW,
            f"isolation-{campaign}",
        )
        for campaign in ("10", "11")
    ]
    monkeypatch.setattr(
        "peermarket_agent.agent.loops.autonomy._eligible_campaigns",
        AsyncMock(
            return_value=[
                {"decision": decisions[0], "draft_id": 1},
                {"decision": decisions[1], "draft_id": 2},
            ]
        ),
    )
    record = AsyncMock(side_effect=[RuntimeError("campaign 10"), object()])
    monkeypatch.setattr("peermarket_agent.agent.loops.autonomy.record_decision", record)
    monkeypatch.setattr("peermarket_agent.agent.loops.autonomy._audit", AsyncMock())
    monkeypatch.setattr("peermarket_agent.agent.loops.autonomy.deliver_pending_outbox", AsyncMock())

    result = await run_autonomy_cycle(
        object(), object(), object(), _limits(meta_autonomy_campaign_ids=("10", "11")), now=NOW
    )

    assert result["failed"] == 1
    assert result["evaluated"] == 1
    assert record.await_count == 2


@pytest.mark.asyncio
async def test_real_single_collected_publication_persists_canonical_input_and_observe(engine):
    performance = {
        "meta": {
            "latest": {
                "impressions": 250,
                "landing_page_views": 25,
                "window_definition": "previous_utc_day",
                "utc_alignment": {
                    "start": (NOW - timedelta(days=1)).isoformat(),
                    "stop_exclusive": NOW.isoformat(),
                },
            },
            "last_successful_retrieval": NOW.isoformat(),
            "error": None,
            "restated": False,
        },
        "delivery": {"condition": "healthy"},
        "attribution": {"available": True, "events": []},
        "autonomy_basis": {
            "campaign_id": "10",
            "external_ids": {"campaign_id": "10", "ad_set_id": "20", "ad_id": "30"},
            "approved_budget_cents": 1000,
            "captured_at": NOW.isoformat(),
            "window_start": (NOW - timedelta(days=1)).isoformat(),
            "window_end": NOW.isoformat(),
            "delivery_state": "healthy",
            "attribution_complete": True,
            "complete": True,
        },
    }
    async with engine.begin() as conn:
        action_type = await conn.scalar(
            text(
                "INSERT INTO action_types(name,risk_tier,default_autonomy) "
                "VALUES ('task7','high','propose') RETURNING id"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO drafts(id,action_type_id,channel,language,status,metadata) "
                "VALUES (156,:type,'meta','MULTI','published',CAST(:metadata AS JSONB))"
            ),
            {"type": action_type, "metadata": json.dumps({"audience_profile_key": "declutterers"})},
        )
        await conn.execute(
            text(
                "INSERT INTO publications(draft_id,channel,state,external_ids,approved_budget_cents,performance) "
                "VALUES (156,'meta','active',CAST(:ids AS JSONB),1000,CAST(:performance AS JSONB))"
            ),
            {
                "ids": json.dumps({"campaign_id": "10", "ad_set_id": "20", "ad_id": "30"}),
                "performance": json.dumps(performance),
            },
        )

    assert await persist_autonomy_inputs(engine) == 1
    result = await run_autonomy_cycle(engine, object(), None, _limits(), now=NOW)

    assert result == {"evaluated": 1, "queued": 0, "executed": 0, "failed": 0}
    async with engine.connect() as conn:
        inputs = await conn.scalar(text("SELECT performance->'autonomy_inputs' FROM publications"))
        decision = (
            (await conn.execute(text("SELECT kind,reason FROM autonomous_decisions")))
            .mappings()
            .one()
        )
        actions = await conn.scalar(text("SELECT count(*) FROM autonomous_actions"))
    assert inputs["schema"] == "autonomy-inputs/v1"
    assert inputs["variants"][0]["variant_id"] == "156"
    assert decision["kind"] == "observe"
    assert actions == 0


@pytest.mark.asyncio
async def test_newer_autonomy_lifecycle_obsoletes_only_undelivered_older_audits(engine):
    await test_real_single_collected_publication_persists_canonical_input_and_observe(engine)
    decision = FrozenDecision(
        DecisionKind.OBSERVE,
        "10",
        {"snapshot_id": "audit"},
        "audit",
        NOW - timedelta(days=1),
        NOW,
        "audit-decision",
    )
    await _audit(engine, draft_id=156, decision=decision, outcome="shadow", detail="first")
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE slack_outbox SET status='failed' WHERE idempotency_key="
                "'autonomy:audit-decision:shadow'"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO drafts(id,action_type_id,channel,language,status) "
                "SELECT 157,action_type_id,'meta','MULTI','published' FROM drafts WHERE id=156"
            )
        )
    await _audit(engine, draft_id=157, decision=decision, outcome="succeeded", detail="recovered")
    await _audit(engine, draft_id=157, decision=decision, outcome="succeeded", detail="recovered")

    async with engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text("SELECT idempotency_key,status FROM slack_outbox ORDER BY id")
                )
            )
            .mappings()
            .all()
        )
    lifecycle = {row["idempotency_key"]: row["status"] for row in rows}
    assert lifecycle["autonomy:audit-decision:shadow"] == "obsolete"
    assert lifecycle["autonomy:audit-decision:succeeded"] == "pending"


@pytest.mark.asyncio
async def test_autonomy_audit_freezes_meaningful_sanitized_campaign_content(engine):
    await test_real_single_collected_publication_persists_canonical_input_and_observe(engine)
    decision = FrozenDecision(
        DecisionKind.SCALE,
        "10",
        {
            "snapshot_id": "audit-content",
            "experiment_id": "draft-156-hook-test",
            "evidence_window": {
                "start": "2026-07-16T12:00:00+00:00",
                "end": "2026-07-17T12:00:00+00:00",
                "captured_at": "2026-07-17T11:30:00+00:00",
            },
            "policy_limits": {"min_impressions": 100, "cooldown_hours": 24},
            "variants": [
                {
                    "variant_id": "NL",
                    "impressions": 250,
                    "landing_page_views": 25,
                    "registrations": 5,
                }
            ],
            "frozen_basis": {
                "campaign_publications": [
                    {"publication_id": 1, "external_ids": {"ad_id": "31", "ad_set_id": "21"}}
                ]
            },
        },
        "proven_winner_scale",
        NOW - timedelta(days=1),
        NOW,
        "audit-content",
        1000,
        1200,
        {
            "1": {
                "publication_id": 1,
                "variant_id": "156",
                "campaign_id": "10",
                "ad_set_id": "21",
                "ad_id": "31",
                "old_budget_cents": 1000,
                "new_budget_cents": 1200,
            }
        },
    )
    await _audit(
        engine,
        draft_id=156,
        decision=decision,
        outcome="succeeded",
        detail="executed",
        rollback_result={"needed": False, "verified": True, "token": "must-not-leak"},
        next_evaluation_at=NOW + timedelta(hours=24),
    )
    async with engine.connect() as conn:
        payload = await conn.scalar(
            text(
                "SELECT payload FROM slack_outbox "
                "WHERE idempotency_key='autonomy:audit-content:succeeded'"
            )
        )

    assert payload["campaign_id"] == "10"
    assert payload["experiment_id"] == "draft-156-hook-test"
    assert payload["variant_ids"] == ["NL"]
    assert payload["evidence_window"]["end"] == "2026-07-17T12:00:00+00:00"
    assert payload["thresholds"] == {"min_impressions": 100, "cooldown_hours": 24}
    assert payload["evidence"][0]["impressions"] == 250
    assert payload["affected_ads"] == [{"ad_id": "31", "ad_set_id": "21", "publication_id": 1}]
    assert payload["budgets"] == {"previous_cents": 1000, "new_cents": 1200}
    assert payload["rollback"] == {"needed": False, "verified": True, "token": "[redacted]"}
    assert "must-not-leak" not in payload["text"]
    assert payload["next_evaluation_at"] == (NOW + timedelta(hours=24)).isoformat()
    assert "thresholds" in payload["text"] and "samples" in payload["text"]
    assert "experiment draft-156-hook-test" in payload["text"]
    assert "evidence window" in payload["text"] and "captured_at" in payload["text"]


@pytest.mark.asyncio
async def test_replacement_success_audit_whitelists_new_bundle_and_retains_source(engine):
    await test_real_single_collected_publication_persists_canonical_input_and_observe(engine)
    decision = FrozenDecision(
        DecisionKind.REPLACE,
        "10",
        {
            "snapshot_id": "replace-audit",
            "policy_limits": {"account_timezone": "Europe/Brussels", "cooldown_hours": 24},
            "variants": [],
            "frozen_basis": {
                "campaign_publications": [
                    {"publication_id": 1, "external_ids": {"ad_set_id": "20", "ad_id": "30"}}
                ]
            },
            "source": {"secret": "not-used"},
        },
        "proven_loser_replace",
        NOW - timedelta(days=1),
        NOW,
        "replace-audit",
    )
    await _audit(
        engine,
        draft_id=156,
        decision=decision,
        outcome="succeeded",
        detail="executed",
        after_state={
            "replacement": {
                "campaign_id": "11",
                "ad_set_id": "21",
                "ad_ids": {"NL": "31", "FR": "32", "EN": "33"},
                "creative_ids": {"NL": "token=leak"},
            },
            "source": {"ad_id": "30", "status": "PAUSED", "access_token": "leak"},
        },
    )
    async with engine.connect() as conn:
        payload = await conn.scalar(
            text(
                "SELECT payload FROM slack_outbox WHERE idempotency_key="
                "'autonomy:replace-audit:succeeded'"
            )
        )
    assert payload["thresholds"]["account_timezone"] == "Europe/Brussels"
    assert payload["replacement_result"] == {
        "campaign_id": "11",
        "ad_set_id": "21",
        "ad_ids": {"NL": "31", "FR": "32", "EN": "33"},
        "source_ad_id": "30",
        "source_status": "PAUSED",
        "changed": "replacement_activated_source_paused",
    }
    assert payload["affected_ads"] == [{"publication_id": 1, "ad_set_id": "20", "ad_id": "30"}]
    assert "token=leak" not in json.dumps(payload)


@pytest.mark.parametrize(
    "scenario",
    ["success", "frozen_drift", "policy_drift", "timezone_drift", "partial_write_failure"],
)
@pytest.mark.asyncio
async def test_three_publication_scale_preserves_allocation_rounding_and_audits(
    engine, monkeypatch, scenario
):
    await test_real_single_collected_publication_persists_canonical_input_and_observe(engine)
    async with engine.begin() as conn:
        first = await conn.scalar(text("SELECT performance FROM publications WHERE draft_id=156"))
        first["attribution"]["events"] = [{"event_type": "registration", "event_count": 1}]
        await conn.execute(
            text("UPDATE drafts SET metadata=CAST(:metadata AS JSONB) WHERE id=156"),
            {
                "metadata": json.dumps(
                    {
                        "experiment_id": "experiment-1",
                        "changed_dimension": "hook",
                        "audience_profile_key": "declutterers",
                    }
                )
            },
        )
        await conn.execute(
            text(
                "UPDATE publications SET performance=CAST(:performance AS JSONB) WHERE draft_id=156"
            ),
            {"performance": json.dumps(first)},
        )
        await conn.execute(
            text(
                "INSERT INTO drafts(id,action_type_id,channel,language,status,metadata) "
                "SELECT 157,action_type_id,'meta','MULTI','published',CAST(:metadata AS JSONB) "
                "FROM drafts WHERE id=156"
            ),
            {
                "metadata": json.dumps(
                    {
                        "experiment_id": "experiment-1",
                        "changed_dimension": "hook",
                        "audience_profile_key": "declutterers",
                    }
                )
            },
        )
        second = json.loads(json.dumps(first))
        second["attribution"]["events"] = [{"event_type": "registration", "event_count": 10}]
        second["autonomy_basis"]["external_ids"] = {
            "campaign_id": "10",
            "ad_set_id": "21",
            "ad_id": "31",
        }
        await conn.execute(
            text(
                "INSERT INTO publications(draft_id,channel,state,external_ids,approved_budget_cents,performance) "
                "VALUES (157,'meta','active',CAST(:ids AS JSONB),1000,CAST(:performance AS JSONB))"
            ),
            {
                "ids": json.dumps({"campaign_id": "10", "ad_set_id": "21", "ad_id": "31"}),
                "performance": json.dumps(second),
            },
        )
        await conn.execute(
            text(
                "INSERT INTO drafts(id,action_type_id,channel,language,status,metadata) "
                "SELECT 158,action_type_id,'meta','MULTI','published',CAST(:metadata AS JSONB) "
                "FROM drafts WHERE id=156"
            ),
            {
                "metadata": json.dumps(
                    {
                        "experiment_id": "experiment-1",
                        "changed_dimension": "hook",
                        "audience_profile_key": "declutterers",
                    }
                )
            },
        )
        unrelated = json.loads(json.dumps(first))
        unrelated["autonomy_basis"]["external_ids"] = {
            "campaign_id": "10",
            "ad_set_id": "22",
            "ad_id": "32",
        }
        await conn.execute(
            text(
                "INSERT INTO publications(draft_id,channel,state,external_ids,approved_budget_cents,performance) "
                "VALUES (158,'meta','active',CAST(:ids AS JSONB),500,CAST(:performance AS JSONB))"
            ),
            {
                "ids": json.dumps({"campaign_id": "10", "ad_set_id": "22", "ad_id": "32"}),
                "performance": json.dumps(unrelated),
            },
        )
        await conn.execute(
            text(
                "INSERT INTO drafts(id,action_type_id,channel,language,status) "
                "SELECT 159,action_type_id,'meta','MULTI','published' FROM drafts WHERE id=156"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO publications(id,draft_id,channel,state,external_ids,approved_budget_cents,performance) "
                "VALUES (0,159,'meta','terminal',CAST(:ids AS JSONB),700,CAST(:performance AS JSONB))"
            ),
            {
                "ids": json.dumps({"campaign_id": "10", "ad_set_id": "19", "ad_id": "29"}),
                "performance": json.dumps(first),
            },
        )
        await conn.execute(
            text(
                "UPDATE publications SET approved_budget_cents=CASE draft_id "
                "WHEN 156 THEN 501 WHEN 157 THEN 499 WHEN 158 THEN 500 "
                "ELSE approved_budget_cents END WHERE draft_id IN (156,157,158)"
            )
        )
    await persist_autonomy_inputs(engine)
    async with engine.begin() as conn:
        canonical = await conn.scalar(
            text("SELECT performance FROM publications WHERE draft_id=156")
        )
        canonical["autonomy_inputs"]["reallocation"] = None
        canonical["autonomy_inputs"]["replacement_source"] = None
        await conn.execute(
            text("UPDATE publications SET performance=CAST(:value AS JSONB) WHERE draft_id=156"),
            {"value": json.dumps(canonical)},
        )
    budgets = {"20": 501, "21": 499, "22": 500}
    writes = []

    async def allocation(config, campaign_id, ad_set_id, ad_id):
        active = {"status": "ACTIVE", "effective_status": "ACTIVE"}
        return {
            "campaign_id": campaign_id,
            "ad_set_id": ad_set_id,
            "ad_id": ad_id,
            "ad_set": active | {"daily_budget": budgets[ad_set_id]},
            "ad": active,
        }

    async def set_budget(config, ad_set_id, cents):
        if scenario == "partial_write_failure" and ad_set_id == "21":
            raise RuntimeError("second write failed")
        budgets[ad_set_id] = cents
        writes.append((ad_set_id, cents))
        return {"daily_budget": cents}

    monkeypatch.setattr("peermarket_agent.autonomy.executor.get_meta_allocation_state", allocation)
    monkeypatch.setattr(
        "peermarket_agent.autonomy.executor.set_meta_adset_daily_budget", set_budget
    )
    settings = _limits(
        meta_autonomy_shadow=False,
        meta_app_id="app",
        meta_app_secret="secret",
        meta_system_user_token="token",
        meta_ad_account_id="act",
        meta_page_id="page",
    )
    if scenario == "frozen_drift":
        from peermarket_agent.autonomy.executor import execute_production_claim

        async def drift_then_execute(db, configured, claude, claim, now):
            async with db.begin() as conn:
                await conn.execute(
                    text("UPDATE publications SET approved_budget_cents=501 WHERE draft_id=158")
                )
            return await execute_production_claim(db, configured, claude, claim, now)

        monkeypatch.setattr(
            "peermarket_agent.agent.loops.autonomy.execute_production_claim", drift_then_execute
        )
    elif scenario == "policy_drift":
        from peermarket_agent.autonomy.executor import execute_production_claim

        async def drift_policy_then_execute(db, configured, claude, claim, now):
            configured.meta_autonomy_max_daily_budget_eur = 19
            return await execute_production_claim(db, configured, claude, claim, now)

        monkeypatch.setattr(
            "peermarket_agent.agent.loops.autonomy.execute_production_claim",
            drift_policy_then_execute,
        )
    elif scenario == "timezone_drift":
        from peermarket_agent.autonomy.executor import execute_production_claim

        async def drift_timezone_then_execute(db, configured, claude, claim, now):
            configured.meta_account_timezone = "UTC"
            return await execute_production_claim(db, configured, claude, claim, now)

        monkeypatch.setattr(
            "peermarket_agent.agent.loops.autonomy.execute_production_claim",
            drift_timezone_then_execute,
        )

    result = await run_autonomy_cycle(engine, object(), None, settings, now=NOW)

    assert result["queued"] == 1
    assert result["executed"] == 1
    if scenario in {"frozen_drift", "policy_drift", "timezone_drift"}:
        assert writes == []
        async with engine.connect() as conn:
            assert (
                await conn.scalar(
                    text("SELECT count(*) FROM autonomous_actions WHERE status='cancelled'")
                )
                == 1
            )
        return
    if scenario == "partial_write_failure":
        assert budgets == {"20": 501, "21": 499, "22": 500}
        assert writes == [("20", 601), ("20", 501)]
        async with engine.connect() as conn:
            assert (
                await conn.scalar(
                    text(
                        "SELECT count(*) FROM autonomous_actions "
                        "WHERE status='reconciliation_required'"
                    )
                )
                == 1
            )
        return
    assert writes == [("20", 601), ("21", 599), ("22", 600)]
    assert sum(budgets.values()) == 1800
    async with engine.connect() as conn:
        assert (
            await conn.scalar(text("SELECT count(*) FROM autonomous_decisions WHERE kind='scale'"))
            >= 1
        )
        assert (
            await conn.scalar(
                text("SELECT count(*) FROM slack_outbox WHERE message_kind='autonomy_audit'")
            )
            >= 1
        )
        assert (
            await conn.scalar(
                text("SELECT count(*) FROM autonomous_actions WHERE status='succeeded'")
            )
            == 1
        )
