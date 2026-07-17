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
    persist_autonomy_inputs,
    run_autonomy_cycle,
)
from peermarket_agent.autonomy.contracts import DecisionKind, FrozenDecision
from peermarket_agent.autonomy.executor import ExecutionStatus
from peermarket_agent.db.migrations import run_migrations

NOW = datetime(2026, 7, 17, 12, tzinfo=UTC)


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
    await _audit(engine, draft_id=156, decision=decision, outcome="succeeded", detail="recovered")
    await _audit(engine, draft_id=156, decision=decision, outcome="succeeded", detail="recovered")

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
async def test_real_qualified_inputs_enqueue_claim_execute_and_audit(engine, monkeypatch):
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
    await persist_autonomy_inputs(engine)
    execute = AsyncMock(
        return_value=SimpleNamespace(
            status=ExecutionStatus.SUCCEEDED,
            reason="executed",
            rollback_result=None,
            retry_at=None,
        )
    )
    monkeypatch.setattr("peermarket_agent.agent.loops.autonomy.execute_production_claim", execute)

    result = await run_autonomy_cycle(
        engine, object(), None, _limits(meta_autonomy_shadow=False), now=NOW
    )

    assert result["queued"] == 1
    assert result["executed"] == 1
    execute.assert_awaited_once()
    async with engine.connect() as conn:
        assert (
            await conn.scalar(
                text("SELECT count(*) FROM autonomous_decisions WHERE kind='reallocate'")
            )
            >= 1
        )
        assert (
            await conn.scalar(
                text("SELECT count(*) FROM slack_outbox WHERE message_kind='autonomy_audit'")
            )
            >= 1
        )
