"""Execution safety contracts for autonomous Meta actions."""

import json
import os
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from peermarket_agent.autonomy.contracts import DecisionKind, FrozenDecision
from peermarket_agent.autonomy.executor import (
    ExecutionStatus,
    MetaExecutionAdapter,
    _bundle_ids,
    _bundle_verified,
    _replace,
    _SagaFailure,
    execute_claim,
)
from peermarket_agent.autonomy.snapshot import build_autonomy_snapshot
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


def test_bundle_boundary_requires_exact_three_ad_and_creative_ids():
    complete = {
        "campaign_id": "10",
        "ad_set_id": "20",
        "ad_ids": {"NL": "31", "FR": "32", "EN": "33"},
        "creative_ids": {"NL": "41", "FR": "42", "EN": "43"},
    }
    assert _bundle_ids(complete)["creative_ids"]["EN"] == "43"
    with pytest.raises(RuntimeError, match="creatives"):
        _bundle_ids(complete | {"creative_ids": None})
    with pytest.raises(RuntimeError, match="locales"):
        _bundle_ids(complete | {"ad_ids": {"NL": "31"}})


class BudgetMeta:
    def __init__(self, **overrides):
        self.budget = overrides.pop("budget_cents", 1000)
        self.overrides = overrides
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
        } | self.overrides

    async def set_budget(self, ad_set_id, cents):
        self.calls.append("set_budget")
        self.budget = cents
        return {"daily_budget": cents}


async def _scale_claim(engine):
    variants = [{"variant_id": "1"}]
    performance = {
        "meta": {
            "latest": {
                "utc_alignment": {
                    "start": datetime(2026, 7, 16, tzinfo=UTC).isoformat(),
                    "stop_exclusive": NOW.isoformat(),
                }
            },
            "last_successful_retrieval": NOW.isoformat(),
            "error": None,
            "restated": False,
        },
        "delivery": {"condition": "healthy"},
        "attribution": {"available": True},
    }
    publication = {
        "external_ids": {"campaign_id": "10", "ad_set_id": "20", "ad_id": "30"},
        "approved_budget_cents": 1000,
        "performance": performance,
    }
    snapshot = build_autonomy_snapshot(
        publication, variants, replacement_source=None, allow_replacement=False
    )
    decision = FrozenDecision(
        kind=DecisionKind.SCALE,
        campaign_id="10",
        evidence={
            "snapshot_id": snapshot["snapshot_id"],
            "delivery_state": "healthy",
            "attribution_complete": True,
            "variants": variants,
        },
        reason="proven winner",
        window_start=snapshot["window_start"],
        window_end=snapshot["window_end"],
        idempotency_key="scale-1",
        old_budget_cents=1000,
        new_budget_cents=1200,
    )
    await enqueue_action(engine, decision)
    claim = await claim_next_action(engine, "worker")
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


def _settings(**overrides):
    values = {
        "meta_autonomy_enabled": True,
        "meta_autonomy_shadow": False,
        "meta_autonomy_campaign_ids": ("10",),
        "meta_autonomy_cooldown_hours": 24,
        "meta_autonomy_max_daily_budget_eur": 20,
        "meta_autonomy_max_increase_percent": 20,
        "performance_snapshot_max_age_hours": 2,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda p: p.pop("meta"), "missing_snapshot"),
        (lambda p: p["meta"].pop("latest"), "missing_snapshot"),
        (
            lambda p: p["meta"].update({"last_successful_retrieval": "2026-07-17T09:59:59+00:00"}),
            "stale_snapshot",
        ),
        (lambda p: p["attribution"].update({"available": False}), "stale_snapshot"),
        (lambda p: p["delivery"].update({"condition": "unknown"}), "stale_snapshot"),
        (lambda p: p["meta"].update({"restated": True}), "stale_snapshot"),
    ],
)
@pytest.mark.asyncio
async def test_execution_cancels_each_missing_or_stale_authoritative_snapshot(
    engine, mutation, reason
):
    claim = await _scale_claim(engine)
    async with engine.begin() as conn:
        performance = await conn.scalar(text("SELECT performance FROM publications"))
        mutation(performance)
        await conn.execute(
            text("UPDATE publications SET performance=CAST(:value AS JSONB)"),
            {"value": json.dumps(performance)},
        )
    meta = BudgetMeta()
    result = await execute_claim(engine, _settings(), meta, None, claim, NOW)
    assert result.status is ExecutionStatus.CANCELLED
    assert result.reason == reason
    assert meta.calls == ["read_source"]


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"meta_autonomy_max_daily_budget_eur": 10}, "budget_cap"),
        ({"meta_autonomy_max_increase_percent": 10}, "increase_cap"),
    ],
)
@pytest.mark.asyncio
async def test_execution_revalidates_exact_budget_caps(engine, overrides, reason):
    claim = await _scale_claim(engine)
    meta = BudgetMeta()
    result = await execute_claim(engine, _settings(**overrides), meta, None, claim, NOW)
    assert result.status is ExecutionStatus.CANCELLED
    assert result.reason == reason
    assert "set_budget" not in meta.calls


@pytest.mark.parametrize(
    "meta",
    [
        BudgetMeta(budget_cents=999),
        BudgetMeta(ad_set_id="999"),
        BudgetMeta(status="PAUSED", effective_status="PAUSED"),
    ],
    ids=["budget", "identity", "status"],
)
@pytest.mark.asyncio
async def test_execution_cancels_each_changed_live_meta_state(engine, meta):
    claim = await _scale_claim(engine)
    result = await execute_claim(engine, _settings(), meta, None, claim, NOW)
    assert result.status is ExecutionStatus.CANCELLED
    assert result.reason == "live_state_changed"
    assert "set_budget" not in meta.calls


class RateLimited(RuntimeError):
    http_status = 429


class ReadRateLimited(BudgetMeta):
    async def read_source(self, campaign_id, ids):
        raise RateLimited("throttled")


class AfterWriteRateLimited(BudgetMeta):
    async def read_source(self, campaign_id, ids):
        if "set_budget" in self.calls:
            raise RateLimited("ambiguous after write")
        return await super().read_source(campaign_id, ids)


@pytest.mark.asyncio
async def test_rate_limit_before_write_releases(engine):
    before_claim = await _scale_claim(engine)
    before = await execute_claim(engine, _settings(), ReadRateLimited(), None, before_claim, NOW)
    assert before.status is ExecutionStatus.RETRYABLE


@pytest.mark.asyncio
async def test_rate_limit_after_write_reconciles(engine):
    after_claim = await _scale_claim(engine)
    after = await execute_claim(
        engine, _settings(), AfterWriteRateLimited(), None, after_claim, NOW
    )
    assert after.status is ExecutionStatus.RECONCILIATION_REQUIRED


def _replace_claim():
    source = {
        "draft_id": 1,
        "publication_id": 2,
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
        "image_prompt": "real image",
        "asset_path": "/tmp/image.png",
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
    decision = FrozenDecision(
        kind=DecisionKind.REPLACE,
        campaign_id="10",
        evidence={"snapshot_id": "s", "source": source},
        reason="replace",
        window_start=datetime(2026, 7, 16, tzinfo=UTC),
        window_end=NOW,
        idempotency_key="replace",
    )
    return SimpleNamespace(
        id=1,
        decision_id=1,
        campaign_id="10",
        kind=DecisionKind.REPLACE,
        decision=decision,
        lease_owner="worker",
        lease_token="token",
    )


class ReplacementMeta:
    def __init__(self, *, source_pause_fails=False, rollback_verified=True):
        self.calls = []
        self.active = False
        self.source_pause_fails = source_pause_fails
        self.rollback_verified = rollback_verified

    async def create_paused(self, **kwargs):
        self.calls.append("create_paused")
        return {
            "campaign_id": "50",
            "ad_set_id": "60",
            "ad_ids": {"NL": "71", "FR": "72", "EN": "73"},
            "creative_ids": {"NL": "81", "FR": "82", "EN": "83"},
        }

    async def read_replacement(self, **kwargs):
        self.calls.append("read_replacement")
        status = "ACTIVE" if self.active else "PAUSED"
        effective = status if self.rollback_verified else "ACTIVE"
        result = {
            key: {"status": status, "effective_status": effective}
            for key in ("campaign", "ad_set", "ad:NL", "ad:FR", "ad:EN")
        }
        result["ad_set"]["daily_budget"] = 1000
        return result

    async def activate_replacement(self, **kwargs):
        self.calls.append("activate_replacement")
        self.active = True

    async def pause_source(self, **kwargs):
        self.calls.append("pause_source")
        if self.source_pause_fails:
            raise RuntimeError("source pause failed")

    async def read_source(self, campaign_id, ids):
        self.calls.append("read_source")
        return {"status": "PAUSED", "effective_status": "PAUSED"}

    async def pause_replacement(self, **kwargs):
        self.calls.append("pause_replacement")
        self.active = False
        return {"paused": True}


class ReplacementBuilder:
    async def build(self, **kwargs):
        return object()


@pytest.mark.asyncio
async def test_replacement_exact_success_order(monkeypatch):
    monkeypatch.setattr("peermarket_agent.autonomy.executor._renew_action", AsyncMock())
    meta = ReplacementMeta()
    after, rollback = await _replace(
        object(),
        object(),
        meta,
        ReplacementBuilder(),
        _replace_claim(),
        {"campaign_id": "10", "ad_set_id": "20", "ad_id": "31"},
    )
    assert rollback is None
    assert after["source"]["status"] == "PAUSED"
    assert meta.calls == [
        "create_paused",
        "read_replacement",
        "activate_replacement",
        "read_replacement",
        "pause_source",
        "read_source",
    ]


@pytest.mark.asyncio
async def test_source_pause_failure_compensates_exactly_once_and_verifies(monkeypatch):
    monkeypatch.setattr("peermarket_agent.autonomy.executor._renew_action", AsyncMock())
    meta = ReplacementMeta(source_pause_fails=True)
    with pytest.raises(_SagaFailure):
        await _replace(
            object(),
            object(),
            meta,
            ReplacementBuilder(),
            _replace_claim(),
            {"campaign_id": "10", "ad_set_id": "20", "ad_id": "31"},
        )
    assert meta.calls.count("pause_replacement") == 1
    assert meta.calls[-2:] == ["pause_replacement", "read_replacement"]


@pytest.mark.asyncio
async def test_source_pause_failure_with_unverified_rollback_is_a_saga_failure(monkeypatch):
    monkeypatch.setattr("peermarket_agent.autonomy.executor._renew_action", AsyncMock())
    meta = ReplacementMeta(source_pause_fails=True, rollback_verified=False)
    with pytest.raises(_SagaFailure) as failure:
        await _replace(
            object(),
            object(),
            meta,
            ReplacementBuilder(),
            _replace_claim(),
            {"campaign_id": "10", "ad_set_id": "20", "ad_id": "31"},
        )
    assert type(failure.value.cause).__name__ == "RuntimeError"
    assert meta.calls.count("pause_replacement") == 1


class MalformedReplacementMeta(ReplacementMeta):
    def __init__(self, verified):
        super().__init__()
        self.verified = verified

    async def create_paused(self, **kwargs):
        self.calls.append("create_paused")
        return {"campaign_id": "50", "ad_set_id": "60", "ad_ids": {"NL": "71"}}

    async def pause_persisted(self, **kwargs):
        self.calls.append("pause_persisted")
        return {"verified": self.verified, "observed": {}}


@pytest.mark.parametrize("verified", [True, False])
@pytest.mark.asyncio
async def test_malformed_bundle_uses_persisted_ids_and_never_claims_unverified_cleanup(
    monkeypatch, verified
):
    monkeypatch.setattr("peermarket_agent.autonomy.executor._renew_action", AsyncMock())
    meta = MalformedReplacementMeta(verified)
    with pytest.raises(_SagaFailure) as failure:
        await _replace(
            object(),
            object(),
            meta,
            ReplacementBuilder(),
            _replace_claim(),
            {"campaign_id": "10", "ad_set_id": "20", "ad_id": "31"},
        )
    assert meta.calls == ["create_paused", "pause_persisted"]
    if not verified:
        assert failure.value.rollback_result["error"] == "RuntimeError"
