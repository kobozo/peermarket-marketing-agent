"""Execution safety contracts for autonomous Meta actions."""

import json
import os
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from peermarket_agent.autonomy.contracts import DecisionKind, FrozenDecision
from peermarket_agent.autonomy.executor import (
    ExecutionStatus,
    MetaExecutionAdapter,
    _bundle_verified,
    execute_claim,
)
from peermarket_agent.autonomy.store import claim_next_action, enqueue_action
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


class Meta:
    def __init__(self):
        self.calls = []

    async def read_source(self, campaign_id):
        self.calls.append("read_source")
        return {
            "campaign_id": campaign_id,
            "ad_set_id": "20",
            "ad_id": "30",
            "budget_cents": 1000,
            "status": "ACTIVE",
            "effective_status": "ACTIVE",
        }


@pytest.mark.asyncio
async def test_shadow_mode_is_impossible_before_meta_or_database_work():
    meta = Meta()
    settings = SimpleNamespace(
        meta_autonomy_enabled=True, meta_autonomy_shadow=True, meta_autonomy_campaign_ids=("10",)
    )
    claim = SimpleNamespace(campaign_id="10")
    result = await execute_claim(None, settings, meta, None, claim, NOW)
    assert result.status is ExecutionStatus.REFUSED
    assert result.reason == "shadow_mode"
    assert meta.calls == []


@pytest.mark.asyncio
async def test_disabled_and_allowlist_refuse_before_meta():
    meta = Meta()
    claim = SimpleNamespace(campaign_id="10")
    disabled = SimpleNamespace(
        meta_autonomy_enabled=False, meta_autonomy_shadow=False, meta_autonomy_campaign_ids=("10",)
    )
    denied = SimpleNamespace(
        meta_autonomy_enabled=True, meta_autonomy_shadow=False, meta_autonomy_campaign_ids=("11",)
    )
    assert (await execute_claim(None, disabled, meta, None, claim, NOW)).reason == "disabled"
    assert (await execute_claim(None, denied, meta, None, claim, NOW)).reason == "not_allowlisted"
    assert meta.calls == []


@pytest.mark.asyncio
async def test_production_adapter_reads_exact_task4_hierarchy_and_budget(monkeypatch):
    adapter = MetaExecutionAdapter.__new__(MetaExecutionAdapter)
    adapter.config = object()

    async def statuses(config, ids):
        assert ids == {"campaign_id": "10", "ad_set_id": "20", "ad_id": "30"}
        return {
            key: {"status": "ACTIVE", "effective_status": "ACTIVE"}
            for key in ("campaign", "ad_set", "ad")
        }

    async def budget(config, ids):
        return {
            "ad": {"status": "ACTIVE", "effective_status": "ACTIVE"},
            "ad_set": {"status": "ACTIVE", "effective_status": "ACTIVE", "daily_budget": 1000},
        }

    monkeypatch.setattr("peermarket_agent.autonomy.executor.get_meta_ad_statuses", statuses)
    monkeypatch.setattr("peermarket_agent.autonomy.executor.get_meta_budget_state", budget)
    result = await adapter.read_source(
        "10", ids={"campaign_id": "10", "ad_set_id": "20", "ad_id": "30"}
    )
    assert result["hierarchy"]["campaign"]["status"] == "ACTIVE"
    assert result["budget_cents"] == 1000


def test_bundle_verifier_requires_exact_complete_hierarchy_and_budget():
    state = {
        key: {"status": "PAUSED", "effective_status": "PAUSED"}
        for key in ("campaign", "ad_set", "ad:NL", "ad:FR", "ad:EN")
    }
    state["ad_set"]["daily_budget"] = 1000
    assert _bundle_verified(state, False, 1000)
    assert not _bundle_verified(state | {"unexpected": {}}, False, 1000)
    assert not _bundle_verified(state, False, 999)


class BudgetMeta:
    def __init__(self):
        self.budget = 1000
        self.calls = []

    async def read_source(self, campaign_id, ids):
        self.calls.append("read_source")
        hierarchy = {
            key: {"status": "ACTIVE", "effective_status": "ACTIVE"}
            for key in ("campaign", "ad_set", "ad")
        }
        return {
            **ids,
            "budget_cents": self.budget,
            "status": "ACTIVE",
            "effective_status": "ACTIVE",
            "hierarchy": hierarchy,
        }

    async def set_budget(self, ad_set_id, cents):
        self.calls.append("set_budget")
        self.budget = cents
        return {"daily_budget": cents}


async def _scale_claim(engine):
    variants = [{"variant_id": "1"}]
    decision = FrozenDecision(
        kind=DecisionKind.SCALE,
        campaign_id="10",
        evidence={
            "snapshot_id": "snap-1",
            "delivery_state": "healthy",
            "attribution_complete": True,
            "variants": variants,
        },
        reason="proven winner",
        window_start=datetime(2026, 7, 16, tzinfo=UTC),
        window_end=NOW,
        idempotency_key="scale-1",
        old_budget_cents=1000,
        new_budget_cents=1200,
    )
    await enqueue_action(engine, decision)
    claim = await claim_next_action(engine, "worker")
    performance = {
        "autonomy_snapshot": {
            "snapshot_id": "snap-1",
            "captured_at": NOW.isoformat(),
            "window_start": decision.window_start.isoformat(),
            "window_end": decision.window_end.isoformat(),
            "complete": True,
            "delivery_state": "healthy",
            "attribution_complete": True,
            "variants": variants,
        }
    }
    async with engine.begin() as conn:
        action_type = await conn.scalar(
            text(
                "INSERT INTO action_types(name,risk_tier,default_autonomy) VALUES ('meta_ad_creative','high','propose') RETURNING id"
            )
        )
        draft = await conn.scalar(
            text(
                "INSERT INTO drafts(action_type_id,channel,language,status) VALUES (:id,'meta','MULTI','published') RETURNING id"
            ),
            {"id": action_type},
        )
        await conn.execute(
            text(
                "INSERT INTO publications(draft_id,channel,state,external_ids,approved_budget_cents,performance) VALUES (:draft,'meta','active',CAST(:ids AS JSONB),1000,CAST(:performance AS JSONB))"
            ),
            {
                "draft": draft,
                "ids": json.dumps({"campaign_id": "10", "ad_set_id": "20", "ad_id": "30"}),
                "performance": json.dumps(performance),
            },
        )
    return claim


@pytest.mark.asyncio
async def test_scale_executes_exact_budget_and_persists_before_intent_after(engine):
    claim = await _scale_claim(engine)
    meta = BudgetMeta()
    settings = SimpleNamespace(
        meta_autonomy_enabled=True,
        meta_autonomy_shadow=False,
        meta_autonomy_campaign_ids=("10",),
        meta_autonomy_cooldown_hours=24,
        meta_autonomy_max_daily_budget_eur=20,
        meta_autonomy_max_increase_percent=20,
        performance_snapshot_max_age_hours=2,
    )
    result = await execute_claim(engine, settings, meta, None, claim, NOW)
    assert result.status is ExecutionStatus.SUCCEEDED
    assert meta.calls == ["read_source", "set_budget", "read_source"]
    async with engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT status,before_state,after_state FROM autonomous_actions WHERE id=:id"
                    ),
                    {"id": claim.id},
                )
            )
            .mappings()
            .one()
        )
    assert row["status"] == "succeeded"
    assert row["before_state"]["intent"]["new_budget_cents"] == 1200
    assert row["after_state"]["budget_cents"] == 1200
