"""Execution safety contracts for autonomous Meta actions."""

import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from peermarket_agent.autonomy.contracts import ActionStatus, DecisionKind, FrozenDecision
from peermarket_agent.autonomy.executor import (
    ExecutionStatus,
    MetaExecutionAdapter,
    _bundle_ids,
    _bundle_verified,
    _replace,
    _SagaFailure,
    execute_claim,
)
from peermarket_agent.autonomy.snapshot import (
    build_autonomy_basis,
    build_autonomy_snapshot,
    build_policy_decision,
)
from peermarket_agent.autonomy.store import (
    begin_execution,
    claim_next_action,
    enqueue_action,
    finish_action,
)
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
        self.pause_readback_fails = overrides.pop("pause_readback_fails", False)
        self.paused = False
        self.overrides = overrides
        self.calls = []

    async def read_source(self, campaign_id, ids):
        self.calls.append("read_source")
        hierarchy = {
            key: {"status": "ACTIVE", "effective_status": "ACTIVE"}
            for key in ("campaign", "ad_set", "ad")
        }
        status = "PAUSED" if self.paused and not self.pause_readback_fails else "ACTIVE"
        hierarchy["ad"] = {"status": status, "effective_status": status}
        return {
            **ids,
            "budget_cents": self.budget,
            "status": status,
            "effective_status": status,
            "hierarchy": hierarchy,
        } | self.overrides

    async def set_budget(self, ad_set_id, cents):
        self.calls.append("set_budget")
        self.budget = cents
        return {"daily_budget": cents}

    async def pause_source(self, source):
        self.calls.append("pause_source")
        self.paused = True
        return {"status": "PAUSED"}


async def _scale_claim(engine, kind=DecisionKind.SCALE):
    allocations = {
        "winner": {
            "campaign_id": "10",
            "variant_id": "1",
            "ad_set_id": "21",
            "ad_id": "31",
            "old_budget_cents": 400,
            "new_budget_cents": 500,
        },
        "loser": {
            "campaign_id": "10",
            "variant_id": "2",
            "ad_set_id": "22",
            "ad_id": "32",
            "old_budget_cents": 600,
            "new_budget_cents": 500,
        },
    }
    variants = (
        [{"variant_id": "1"}, {"variant_id": "2"}]
        if kind is DecisionKind.REALLOCATE
        else [{"variant_id": "1"}]
    )
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
    performance["autonomy_basis"] = build_autonomy_basis(publication, performance)
    snapshot = build_autonomy_snapshot(
        publication,
        variants,
        replacement_source=None,
        allow_replacement=False,
        reallocation=(
            {"old_budget_cents": 1000, "new_budget_cents": 1000, "allocations": allocations}
            if kind is DecisionKind.REALLOCATE
            else None
        ),
    )
    decision = FrozenDecision(
        kind=kind,
        campaign_id="10",
        evidence={
            "snapshot_id": snapshot["snapshot_id"],
            "delivery_state": "healthy",
            "attribution_complete": True,
            "variants": variants,
            "frozen_basis": snapshot["frozen_basis"],
        },
        reason="proven winner",
        window_start=snapshot["window_start"],
        window_end=snapshot["window_end"],
        idempotency_key=f"{kind.value}-1",
        old_budget_cents=1000 if kind in {DecisionKind.SCALE, DecisionKind.REALLOCATE} else None,
        new_budget_cents=(
            1200
            if kind is DecisionKind.SCALE
            else 1000
            if kind is DecisionKind.REALLOCATE
            else None
        ),
        allocations=allocations if kind is DecisionKind.REALLOCATE else None,
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
    assert meta.calls == ["read_source", "read_source", "set_budget", "read_source"]
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


@pytest.mark.asyncio
async def test_public_pause_success_persists_verified_readback(engine):
    claim = await _scale_claim(engine, DecisionKind.PAUSE)
    meta = BudgetMeta()
    result = await execute_claim(engine, _settings(), meta, None, claim, NOW)
    assert result.status is ExecutionStatus.SUCCEEDED
    async with engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text("SELECT status,after_state FROM autonomous_actions WHERE id=:id"),
                    {"id": claim.id},
                )
            )
            .mappings()
            .one()
        )
    assert row["status"] == "succeeded"
    assert row["after_state"]["status"] == "PAUSED"


@pytest.mark.asyncio
async def test_public_pause_readback_failure_requires_reconciliation(engine):
    claim = await _scale_claim(engine, DecisionKind.PAUSE)
    meta = BudgetMeta(pause_readback_fails=True)
    result = await execute_claim(engine, _settings(), meta, None, claim, NOW)
    assert result.status is ExecutionStatus.RECONCILIATION_REQUIRED
    async with engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text("SELECT status,failure_message FROM autonomous_actions WHERE id=:id"),
                    {"id": claim.id},
                )
            )
            .mappings()
            .one()
        )
    assert row == {
        "status": "reconciliation_required",
        "failure_message": "pause_verification_failed",
    }


class ReallocationMeta(BudgetMeta):
    def __init__(self, fail_write=None, rollback_unproven=False):
        super().__init__()
        self.fail_writes = set(fail_write if isinstance(fail_write, tuple) else (fail_write,))
        self.rollback_unproven = rollback_unproven
        self.writes = 0
        self.budgets = {"21": 400, "22": 600}

    async def read_allocation(self, allocation):
        budget = self.budgets[allocation["ad_set_id"]]
        if self.rollback_unproven and self.writes > 2:
            budget = allocation["new_budget_cents"]
        active = {"status": "ACTIVE", "effective_status": "ACTIVE"}
        return dict(allocation) | {"ad_set": active | {"daily_budget": budget}, "ad": active}

    async def set_budget(self, ad_set_id, cents):
        self.writes += 1
        if self.writes in self.fail_writes:
            raise RuntimeError("injected write failure")
        self.budgets[ad_set_id] = cents
        return {"daily_budget": cents}


@pytest.mark.parametrize(
    ("fail_write", "rollback_unproven", "expected"),
    [
        (None, False, ExecutionStatus.SUCCEEDED),
        (1, False, ExecutionStatus.RECONCILIATION_REQUIRED),
        (2, False, ExecutionStatus.RECONCILIATION_REQUIRED),
        (2, True, ExecutionStatus.RECONCILIATION_REQUIRED),
        ((2, 3), False, ExecutionStatus.RECONCILIATION_REQUIRED),
    ],
)
@pytest.mark.asyncio
async def test_public_reallocation_write_and_rollback_matrix(
    engine, fail_write, rollback_unproven, expected
):
    claim = await _scale_claim(engine, DecisionKind.REALLOCATE)
    result = await execute_claim(
        engine, _settings(), ReallocationMeta(fail_write, rollback_unproven), None, claim, NOW
    )
    assert result.status is expected
    async with engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT status,before_state,after_state,audit,failure_category FROM autonomous_actions WHERE id=:id"
                    ),
                    {"id": claim.id},
                )
            )
            .mappings()
            .one()
        )
    assert row["before_state"]["intent"]["kind"] == "reallocate"
    if expected is ExecutionStatus.SUCCEEDED:
        assert row["after_state"]["allocations"]["winner"]["ad_set"]["daily_budget"] == 500
    else:
        assert row["status"] == "reconciliation_required"
        assert "rollback_result" in row["audit"]


@pytest.mark.asyncio
async def test_public_execution_rechecks_cooldown_race_after_claim(engine):
    claim = await _scale_claim(engine)
    async with engine.begin() as conn:
        decision_id = await conn.scalar(
            text(
                "INSERT INTO autonomous_decisions(decision_key,kind,campaign_id,window_start,window_end,evidence,reason) "
                "VALUES ('race-history','pause','10',NOW()-INTERVAL '2 hours',NOW()-INTERVAL '1 hour','{}','race') RETURNING id"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO autonomous_actions(decision_id,campaign_id,status,updated_at) "
                "VALUES (:decision,'10','succeeded',NOW())"
            ),
            {"decision": decision_id},
        )
    meta = BudgetMeta()
    result = await execute_claim(engine, _settings(), meta, None, claim, NOW)
    assert result.status is ExecutionStatus.CANCELLED
    assert result.reason == "cooldown"
    assert meta.calls == ["read_source"]


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
        (lambda p: p.pop("autonomy_basis"), "missing_snapshot"),
        (
            lambda p: p["autonomy_basis"].update({"captured_at": "2026-07-17T09:59:59+00:00"}),
            "frozen_basis_changed",
        ),
        (
            lambda p: p["autonomy_basis"].update({"attribution_complete": False}),
            "frozen_basis_changed",
        ),
        (
            lambda p: p["autonomy_basis"].update({"delivery_state": "unknown"}),
            "frozen_basis_changed",
        ),
        (lambda p: p["autonomy_basis"].update({"complete": False}), "frozen_basis_changed"),
        (
            lambda p: p["autonomy_basis"].update({"window_end": "2026-07-17T11:00:00+00:00"}),
            "frozen_basis_changed",
        ),
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


@pytest.mark.asyncio
async def test_database_and_live_meta_drifting_together_still_rejects_frozen_basis(engine):
    claim = await _scale_claim(engine)
    changed_ids = {"campaign_id": "10", "ad_set_id": "999", "ad_id": "998"}
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE publications SET external_ids=CAST(:ids AS JSONB), approved_budget_cents=1500"
            ),
            {"ids": json.dumps(changed_ids)},
        )
    meta = BudgetMeta(budget_cents=1500, ad_set_id="999", ad_id="998")
    result = await execute_claim(engine, _settings(), meta, None, claim, NOW)
    assert result.status is ExecutionStatus.CANCELLED
    assert result.reason == "frozen_basis_changed"
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
        evidence={
            "snapshot_id": "s",
            "source": source,
            "frozen_basis": {
                "external_ids": {"campaign_id": "10", "ad_set_id": "20", "ad_id": "31"},
                "approved_budget_cents": 1000,
            },
        },
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
    def __init__(
        self,
        *,
        source_pause_fails=False,
        rollback_verified=True,
        activation_fails=False,
        create_fails=False,
    ):
        self.calls = []
        self.active = False
        self.source_pause_fails = source_pause_fails
        self.rollback_verified = rollback_verified
        self.source_paused = False
        self.rollback_phase = False
        self.activation_fails = activation_fails
        self.create_fails = create_fails

    async def create_paused(self, **kwargs):
        self.calls.append("create_paused")
        if self.create_fails:
            raise RuntimeError("ambiguous create failure")
        return {
            "campaign_id": "50",
            "ad_set_id": "60",
            "ad_ids": {"NL": "71", "FR": "72", "EN": "73"},
            "creative_ids": {"NL": "81", "FR": "82", "EN": "83"},
        }

    async def read_replacement(self, **kwargs):
        self.calls.append("read_replacement")
        status = "ACTIVE" if self.active else "PAUSED"
        effective = "ACTIVE" if self.rollback_phase and not self.rollback_verified else status
        result = {
            key: {"status": status, "effective_status": effective}
            for key in ("campaign", "ad_set", "ad:NL", "ad:FR", "ad:EN")
        }
        result["ad_set"]["daily_budget"] = 1000
        return result

    async def activate_replacement(self, **kwargs):
        self.calls.append("activate_replacement")
        self.active = True
        if self.activation_fails:
            raise RuntimeError("activation split failure")

    async def pause_source(self, **kwargs):
        self.calls.append("pause_source")
        if self.source_pause_fails:
            raise RuntimeError("source pause failed")
        self.source_paused = True

    async def read_source(self, campaign_id, ids):
        self.calls.append("read_source")
        status = "PAUSED" if self.source_paused else "ACTIVE"
        return {
            **ids,
            "budget_cents": 1000,
            "status": status,
            "effective_status": status,
            "hierarchy": {
                "campaign": {"status": "ACTIVE", "effective_status": "ACTIVE"},
                "ad_set": {"status": "ACTIVE", "effective_status": "ACTIVE"},
                "ad": {"status": status, "effective_status": status},
            },
        }

    async def pause_replacement(self, **kwargs):
        self.calls.append("pause_replacement")
        self.active = False
        self.rollback_phase = True
        return {"paused": True}

    async def pause_persisted(self, **kwargs):
        self.calls.append("pause_persisted")
        return {"verified": self.rollback_verified, "observed": {"persisted": True}}


class ReplacementBuilder:
    async def build(self, **kwargs):
        return object()


async def _public_replace_claim(engine):
    decision = await _canonical_policy_replace(engine)
    await enqueue_action(engine, decision)
    return await claim_next_action(engine, "worker", lease_seconds=300)


@pytest.mark.asyncio
async def test_public_replace_source_pause_unverified_rollback_persists_exact_audit(
    engine, monkeypatch
):
    claim = await _public_replace_claim(engine)
    meta = ReplacementMeta(source_pause_fails=True, rollback_verified=False)

    result = await execute_claim(engine, _settings(), meta, ReplacementBuilder(), claim, NOW)

    assert result.status is ExecutionStatus.RECONCILIATION_REQUIRED
    async with engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT status,failure_category,failure_message,audit "
                        "FROM autonomous_actions WHERE id=:id"
                    ),
                    {"id": claim.id},
                )
            )
            .mappings()
            .one()
        )
    assert row["status"] == "reconciliation_required"
    assert row["failure_category"] == "replacement_rollback_unproven"
    assert row["failure_message"] == "replacement_rollback_unproven"
    rollback = row["audit"]["rollback_result"]
    assert rollback["verified"] is False
    assert rollback["observed"]["ad:NL"]["effective_status"] == "ACTIVE"

    later = FrozenDecision(
        kind=DecisionKind.PAUSE,
        campaign_id="10",
        evidence={"blocked_by": claim.id},
        reason="later action",
        window_start=datetime(2026, 7, 16, tzinfo=UTC),
        window_end=NOW,
        idempotency_key="later-after-reconciliation",
    )
    blocked = await enqueue_action(engine, later)
    assert blocked.created is False
    assert blocked.id == claim.id
    assert blocked.status.value == "reconciliation_required"


@pytest.mark.asyncio
async def test_public_replace_success_persists_full_final_audit(engine, monkeypatch):
    claim = await _public_replace_claim(engine)
    meta = ReplacementMeta()

    result = await execute_claim(engine, _settings(), meta, ReplacementBuilder(), claim, NOW)

    assert result.status is ExecutionStatus.SUCCEEDED
    assert meta.calls == [
        "read_source",
        "read_source",
        "create_paused",
        "read_replacement",
        "read_replacement",
        "activate_replacement",
        "read_replacement",
        "read_source",
        "pause_source",
        "read_source",
    ]
    async with engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT status,before_state,after_state,audit "
                        "FROM autonomous_actions WHERE id=:id"
                    ),
                    {"id": claim.id},
                )
            )
            .mappings()
            .one()
        )
    assert row["status"] == "succeeded"
    assert row["before_state"]["intent"]["kind"] == "replace"
    assert row["after_state"]["replacement"]["ad_set"]["daily_budget"] == 1000
    assert row["after_state"]["source"]["status"] == "PAUSED"
    assert row["audit"] == {}


@pytest.mark.parametrize(
    ("meta", "category", "verified"),
    [
        (ReplacementMeta(source_pause_fails=True), "external_state_unproven", True),
        (ReplacementMeta(activation_fails=True), "external_state_unproven", True),
    ],
)
@pytest.mark.asyncio
async def test_public_replace_split_failures_persist_proven_compensation(
    engine, monkeypatch, meta, category, verified
):
    claim = await _public_replace_claim(engine)
    result = await execute_claim(engine, _settings(), meta, ReplacementBuilder(), claim, NOW)
    assert result.status is ExecutionStatus.RECONCILIATION_REQUIRED
    async with engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text("SELECT failure_category,audit FROM autonomous_actions WHERE id=:id"),
                    {"id": claim.id},
                )
            )
            .mappings()
            .one()
        )
    assert row["failure_category"] == category
    assert row["audit"]["rollback_result"]["verified"] is verified
    assert meta.calls.count("pause_replacement") == 1


@pytest.mark.asyncio
async def test_public_ambiguous_create_uses_persisted_adoption_cleanup_and_audit(
    engine, monkeypatch
):
    claim = await _public_replace_claim(engine)
    meta = ReplacementMeta(create_fails=True)
    result = await execute_claim(engine, _settings(), meta, ReplacementBuilder(), claim, NOW)
    assert result.status is ExecutionStatus.RECONCILIATION_REQUIRED
    assert meta.calls == ["read_source", "read_source", "create_paused", "pause_persisted"]
    async with engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text("SELECT failure_category,audit FROM autonomous_actions WHERE id=:id"),
                    {"id": claim.id},
                )
            )
            .mappings()
            .one()
        )
    assert row["failure_category"] == "external_state_unproven"
    assert row["audit"]["rollback_result"] == {
        "verified": True,
        "observed": {"persisted": True},
    }


class LeaseStealingReplacementMeta(ReplacementMeta):
    async def create_paused(self, **kwargs):
        bundle = await super().create_paused(**kwargs)
        async with kwargs["engine"].begin() as conn:
            await conn.execute(
                text("UPDATE autonomous_actions SET lease_token='stolen' WHERE id=:id"),
                {"id": kwargs["claim"].id},
            )
        return bundle


@pytest.mark.asyncio
async def test_public_lease_loss_between_replacement_writes_prevents_stale_finish(
    engine, monkeypatch
):
    claim = await _public_replace_claim(engine)
    meta = LeaseStealingReplacementMeta()
    result = await execute_claim(engine, _settings(), meta, ReplacementBuilder(), claim, NOW)
    assert result.status is ExecutionStatus.FAILED
    assert result.reason == "lease_lost"
    assert meta.calls == ["read_source", "read_source", "create_paused"]
    async with engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT status,lease_token,after_state FROM autonomous_actions WHERE id=:id"
                    ),
                    {"id": claim.id},
                )
            )
            .mappings()
            .one()
        )
    assert row["status"] == "executing"
    assert row["lease_token"] == "stolen"
    assert row["after_state"] == {}


class BoundaryLeaseReplacementMeta(ReplacementMeta):
    def __init__(self, engine, claim, steal_on_read):
        super().__init__()
        self.engine = engine
        self.claim = claim
        self.steal_on_read = steal_on_read
        self.reads = 0

    async def read_source(self, campaign_id, ids):
        result = await super().read_source(campaign_id, ids)
        self.reads += 1
        if self.reads == self.steal_on_read:
            async with self.engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE autonomous_actions SET lease_token='boundary-stolen' WHERE id=:id"
                    ),
                    {"id": self.claim.id},
                )
        return result


@pytest.mark.parametrize(
    ("steal_on_read", "expected_writes"),
    [(1, []), (4, ["create_paused", "activate_replacement", "pause_source"])],
)
@pytest.mark.asyncio
async def test_public_lease_loss_before_first_write_and_at_finalization_boundary(
    engine, monkeypatch, steal_on_read, expected_writes
):
    claim = await _public_replace_claim(engine)
    meta = BoundaryLeaseReplacementMeta(engine, claim, steal_on_read)
    result = await execute_claim(engine, _settings(), meta, ReplacementBuilder(), claim, NOW)
    assert result.status is ExecutionStatus.FAILED
    assert result.reason == "lease_lost"
    assert [call for call in meta.calls if call in expected_writes] == expected_writes
    async with engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT status,lease_token,after_state FROM autonomous_actions WHERE id=:id"
                    ),
                    {"id": claim.id},
                )
            )
            .mappings()
            .one()
        )
    assert row["status"] == "executing"
    assert row["lease_token"] == "boundary-stolen"
    assert row["after_state"] == {}


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
        "read_source",
        "create_paused",
        "read_replacement",
        "read_replacement",
        "activate_replacement",
        "read_replacement",
        "read_source",
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
    assert meta.calls == ["read_source", "create_paused", "pause_persisted"]
    if not verified:
        assert failure.value.rollback_result["error"] == "RuntimeError"


def _policy_limits():
    return {
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


async def _canonical_policy_replace(engine):
    source = _replace_claim().decision.evidence["source"]
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
        "external_ids": {"campaign_id": "10", "ad_set_id": "20", "ad_id": "31"},
        "approved_budget_cents": 1000,
        "performance": performance,
    }
    performance["autonomy_basis"] = build_autonomy_basis(publication, performance)
    common = {
        "channel": "meta",
        "objective": "OUTCOME_TRAFFIC",
        "language": "MULTI",
        "audience": "declutterers",
        "creative_dimension": "hook",
        "window_definition": "previous_utc_day",
        "impressions": 1000,
        "landing_page_views": 100,
    }
    variants = [
        common | {"variant_id": "loser", "publication_id": 2, "registrations": 1},
        common | {"variant_id": "winner", "publication_id": 3, "registrations": 10},
    ]
    decision = build_policy_decision(
        publication,
        variants,
        replacement_source=source,
        history=(),
        limits=_policy_limits(),
        now=NOW,
    )
    assert decision.kind is DecisionKind.REPLACE
    assert decision.reason == "proven_loser_replace"
    async with engine.begin() as conn:
        action_type = await conn.scalar(
            text(
                "INSERT INTO action_types(name,risk_tier,default_autonomy) "
                "VALUES ('canonical_replace','high','propose') RETURNING id"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO drafts(id,action_type_id,channel,language,status,metadata) "
                "VALUES (1,:type,'meta','MULTI','published',CAST(:metadata AS JSONB))"
            ),
            {
                "type": action_type,
                "metadata": json.dumps(
                    {
                        "experiment_id": source["experiment_id"],
                        "changed_dimension": source["changed_dimension"],
                        "locales": source["locales"],
                        "audience_profile_key": source["audience_profile_key"],
                        "image_prompt": source["image_prompt"],
                        "asset_path": source["asset_path"],
                        "suggested_daily_budget_eur": 10,
                        "landing_page_url": source["landing_page_url"],
                    }
                ),
            },
        )
        await conn.execute(
            text(
                "INSERT INTO publications(id,draft_id,channel,state,external_ids,"
                "approved_budget_cents,performance) VALUES "
                "(2,1,'meta','active',CAST(:ids AS JSONB),1000,CAST(:performance AS JSONB))"
            ),
            {
                "ids": json.dumps(publication["external_ids"]),
                "performance": json.dumps(performance),
            },
        )
    return decision


@pytest.mark.asyncio
async def test_policy_replace_is_cancelled_when_replacement_count_changes_before_execution(engine):
    decision = await _canonical_policy_replace(engine)
    prior = FrozenDecision(
        DecisionKind.REPLACE,
        "10",
        {"prior": True},
        "prior successful replacement",
        datetime(2026, 7, 16, tzinfo=UTC),
        NOW,
        "replacement-completed-after-policy-decision",
    )
    await enqueue_action(engine, prior)
    prior_claim = await claim_next_action(engine, "history-worker")
    assert await begin_execution(engine, prior_claim)
    assert await finish_action(engine, prior_claim, status=ActionStatus.SUCCEEDED)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE autonomous_actions SET updated_at=:at WHERE id=:id"),
            {"at": NOW - timedelta(minutes=1), "id": prior_claim.id},
        )
    await enqueue_action(engine, decision)
    claim = await claim_next_action(engine, "executor-worker")
    meta = ReplacementMeta()

    result = await execute_claim(
        engine, _settings(meta_autonomy_cooldown_hours=0), meta, None, claim, NOW
    )

    assert result.status is ExecutionStatus.CANCELLED
    assert result.reason == "replacement_limit"
    assert meta.calls == ["read_source"]
    async with engine.connect() as conn:
        assert (
            await conn.scalar(
                text("SELECT status FROM autonomous_actions WHERE id=:id"), {"id": claim.id}
            )
            == "cancelled"
        )


@pytest.mark.asyncio
async def test_duplicate_retry_adopts_task5_paused_bundle_without_regeneration_or_creation(
    engine, monkeypatch
):
    decision = await _canonical_policy_replace(engine)
    await enqueue_action(engine, decision)
    claim = await claim_next_action(engine, "worker", lease_seconds=300)
    source = decision.evidence["source"]
    locales = {
        locale: value
        | {
            "body": f"replacement {locale}",
            "audience_profile_key": source["audience_profile_key"],
            "image_prompt": source["image_prompt"],
            "asset_path": source["asset_path"],
        }
        for locale, value in source["locales"].items()
    }
    landing = "https://peermarket.eu/?utm_source=facebook&utm_medium=paid_social&utm_campaign=peermarket&utm_content=draft-3"
    progress = {
        "campaign_id": "50",
        "ad_set_id": "60",
        "ads_manager_url": "https://business.facebook.com/adsmanager/manage/campaigns?act=1",
        **{f"ad_id:{k}": v for k, v in {"NL": "71", "FR": "72", "EN": "73"}.items()},
        **{f"creative_id:{k}": v for k, v in {"NL": "81", "FR": "82", "EN": "83"}.items()},
    }
    async with engine.begin() as conn:
        action_type = await conn.scalar(text("SELECT id FROM action_types LIMIT 1"))
        replacement_id = await conn.scalar(
            text(
                "INSERT INTO drafts(id,action_type_id,channel,language,status,copy,generation_cost_cents,"
                "brand_score,visual_truthfulness_pass,metadata) VALUES "
                "(3,:type,'meta','MULTI','approved','bundle',3,91,true,CAST(:metadata AS JSONB)) RETURNING id"
            ),
            {
                "type": action_type,
                "metadata": json.dumps(
                    {
                        "autonomous_replacement": True,
                        "source_draft_id": 1,
                        "source_campaign_id": "10",
                        "experiment_id": source["experiment_id"],
                        "changed_dimension": source["changed_dimension"],
                        "locales": locales,
                        "audience_profile_key": source["audience_profile_key"],
                        "image_prompt": source["image_prompt"],
                        "asset_path": source["asset_path"],
                        "suggested_daily_budget_eur": 10,
                        "landing_page_url": landing,
                    }
                ),
            },
        )
        await conn.execute(
            text(
                "INSERT INTO autonomous_replacement_generations"
                "(action_id,state,replacement_draft_id) VALUES (:action,'completed',:draft)"
            ),
            {"action": claim.id, "draft": replacement_id},
        )
        await conn.execute(
            text(
                "INSERT INTO autonomous_replacement_publications"
                "(action_id,replacement_draft_id,source_draft_id,state,frozen_budget_cents,"
                "source_campaign_id,changed_dimension,landing_page_url,progress) VALUES "
                "(:action,:draft,1,'paused',1000,'10','hook',:url,CAST(:progress AS JSONB))"
            ),
            {
                "action": claim.id,
                "draft": replacement_id,
                "url": landing,
                "progress": json.dumps(progress),
            },
        )

    paused = {
        key: {"status": "PAUSED", "effective_status": "PAUSED"}
        for key in ("campaign", "ad_set", "ad:NL", "ad:FR", "ad:EN")
    }
    paused["ad_set"]["daily_budget"] = 1000
    active_locales = set()
    source_paused = False
    task5_read = AsyncMock(return_value=paused)
    create = AsyncMock(side_effect=AssertionError("must adopt persisted Task5 bundle"))
    monkeypatch.setattr(
        "peermarket_agent.meta_pipeline.get_meta_replacement_bundle_statuses", task5_read
    )
    monkeypatch.setattr(
        "peermarket_agent.meta_pipeline.create_meta_replacement_bundle_paused", create
    )

    async def read_replacement(*args, **kwargs):
        observed = {
            key: {"status": "ACTIVE", "effective_status": "ACTIVE"}
            for key in ("campaign", "ad_set")
        }
        if not active_locales:
            observed["campaign"] = observed["ad_set"] = {
                "status": "PAUSED",
                "effective_status": "PAUSED",
            }
        for locale in ("NL", "FR", "EN"):
            state = "ACTIVE" if locale in active_locales else "PAUSED"
            observed[f"ad:{locale}"] = {"status": state, "effective_status": state}
        observed["ad_set"]["daily_budget"] = 1000
        return observed

    async def activate(*args, **kwargs):
        active_locales.add("NL")
        return {"status": "ACTIVE"}

    async def set_status(config, ad_id, status):
        nonlocal source_paused
        if ad_id == "31":
            source_paused = status == "PAUSED"
        elif status == "ACTIVE":
            active_locales.add({"72": "FR", "73": "EN"}[ad_id])
        return {"status": status}

    async def source_statuses(config, ids):
        status = "PAUSED" if source_paused else "ACTIVE"
        return {
            "campaign": {"status": "ACTIVE", "effective_status": "ACTIVE"},
            "ad_set": {"status": "ACTIVE", "effective_status": "ACTIVE"},
            "ad": {"status": status, "effective_status": status},
        }

    async def budget(*args, **kwargs):
        return {
            "ad": {"status": "ACTIVE", "effective_status": "ACTIVE"},
            "ad_set": {"status": "ACTIVE", "effective_status": "ACTIVE", "daily_budget": 1000},
        }

    monkeypatch.setattr(
        "peermarket_agent.autonomy.executor.get_meta_replacement_bundle_statuses", read_replacement
    )
    monkeypatch.setattr("peermarket_agent.autonomy.executor.activate_meta_ad", activate)
    monkeypatch.setattr("peermarket_agent.autonomy.executor.set_meta_ad_status", set_status)
    monkeypatch.setattr("peermarket_agent.autonomy.executor.get_meta_ad_statuses", source_statuses)
    monkeypatch.setattr("peermarket_agent.autonomy.executor.get_meta_budget_state", budget)

    class ClaudeMustNotRun:
        async def complete(self, *args, **kwargs):
            raise AssertionError("completed generation must be adopted")

    settings = _settings(
        meta_app_id="app",
        meta_app_secret="secret",
        meta_system_user_token="token",
        meta_ad_account_id="act",
        meta_page_id="page",
    )
    result = await execute_claim(
        engine, settings, MetaExecutionAdapter(settings, ClaudeMustNotRun()), None, claim, NOW
    )

    assert result.status is ExecutionStatus.SUCCEEDED
    assert result.after_state["replacement"]["ad_set"]["daily_budget"] == 1000
    create.assert_not_awaited()
    task5_read.assert_awaited_once()
    assert source_paused and active_locales == {"NL", "FR", "EN"}
    async with engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text("SELECT status,audit FROM autonomous_actions WHERE id=:id"),
                    {"id": claim.id},
                )
            )
            .mappings()
            .one()
        )
        assert row["status"] == "succeeded"
        stored_progress = await conn.scalar(
            text("SELECT progress FROM autonomous_replacement_publications WHERE action_id=:id"),
            {"id": claim.id},
        )
        assert stored_progress["campaign_id"] == "50"
        assert {key: stored_progress[f"ad_id:{key}"] for key in ("NL", "FR", "EN")} == {
            "NL": "71",
            "FR": "72",
            "EN": "73",
        }
        assert await conn.scalar(text("SELECT count(*) FROM drafts")) == 2
        assert (
            await conn.scalar(text("SELECT count(*) FROM autonomous_replacement_generations")) == 1
        )
        assert (
            await conn.scalar(text("SELECT count(*) FROM autonomous_replacement_publications")) == 1
        )


class FounderChangedBeforePause(BudgetMeta):
    def __init__(self):
        super().__init__()
        self.reads = 0

    async def read_source(self, campaign_id, ids):
        self.reads += 1
        result = await super().read_source(campaign_id, ids)
        if self.reads == 2:
            result["status"] = result["effective_status"] = "PAUSED"
            result["hierarchy"]["ad"] = {"status": "PAUSED", "effective_status": "PAUSED"}
        return result


@pytest.mark.asyncio
async def test_pause_fresh_reads_target_immediately_before_mutation_and_founder_change_wins(engine):
    claim = await _scale_claim(engine, DecisionKind.PAUSE)
    meta = FounderChangedBeforePause()

    result = await execute_claim(engine, _settings(), meta, None, claim, NOW)

    assert result.status is ExecutionStatus.CANCELLED
    assert result.reason == "live_state_changed"
    assert meta.calls == ["read_source", "read_source"]


class FounderChangedBeforeScale(BudgetMeta):
    def __init__(self):
        super().__init__()
        self.reads = 0

    async def read_source(self, campaign_id, ids):
        self.reads += 1
        result = await super().read_source(campaign_id, ids)
        if self.reads == 2:
            result["budget_cents"] = 1100
        return result


@pytest.mark.asyncio
async def test_scale_fresh_reads_budget_immediately_before_mutation(engine):
    claim = await _scale_claim(engine)
    meta = FounderChangedBeforeScale()

    result = await execute_claim(engine, _settings(), meta, None, claim, NOW)

    assert result.status is ExecutionStatus.CANCELLED
    assert result.reason == "live_state_changed"
    assert meta.calls == ["read_source", "read_source"]


@pytest.mark.parametrize(
    ("failure_stage", "expected_active"),
    [
        ("after_nl", {"NL"}),
        ("after_fr", {"NL", "FR"}),
        ("during_en", {"NL", "FR"}),
    ],
)
@pytest.mark.asyncio
async def test_production_adapter_fences_each_locale_activation_stage(
    monkeypatch, failure_stage, expected_active
):
    adapter = MetaExecutionAdapter.__new__(MetaExecutionAdapter)
    adapter.config = object()
    adapter._drafts = {"50": SimpleNamespace(daily_budget_eur=10)}
    adapter._identities = {}
    active = set()

    async def read_replacement(*args, **kwargs):
        observed = {
            key: {
                "status": "ACTIVE" if key in active else "PAUSED",
                "effective_status": "ACTIVE" if key in active else "PAUSED",
            }
            for key in ("NL", "FR", "EN")
        }
        result = {f"ad:{key}": value for key, value in observed.items()}
        hierarchy = "ACTIVE" if active else "PAUSED"
        result["campaign"] = {"status": hierarchy, "effective_status": hierarchy}
        result["ad_set"] = {
            "status": hierarchy,
            "effective_status": hierarchy,
            "daily_budget": 1000,
        }
        return result

    adapter.read_replacement = read_replacement

    async def activate(*args, **kwargs):
        active.add("NL")
        if failure_stage == "after_nl":
            raise RuntimeError("after NL")
        return {"status": "ACTIVE"}

    async def set_status(config, ad_id, status):
        locale = {"72": "FR", "73": "EN"}[ad_id]
        if failure_stage == "during_en" and locale == "EN":
            raise RuntimeError("during EN")
        active.add(locale)
        if failure_stage == "after_fr" and locale == "FR":
            raise RuntimeError("after FR")
        return {"status": status}

    monkeypatch.setattr("peermarket_agent.autonomy.executor.activate_meta_ad", activate)
    monkeypatch.setattr("peermarket_agent.autonomy.executor.set_meta_ad_status", set_status)

    with pytest.raises(RuntimeError):
        await adapter.activate_replacement("50", "60", {"NL": "71", "FR": "72", "EN": "73"})
    assert active == expected_active


class CheckpointProxy:
    def __init__(self, target, checkpoint, *, transient=False):
        self.target = target
        self.checkpoint = checkpoint
        self.transient = transient
        self.counts = {}

    def __getattr__(self, name):
        value = getattr(self.target, name)
        if not callable(value):
            return value

        async def call(*args, **kwargs):
            self.counts[name] = self.counts.get(name, 0) + 1
            if f"{name}:{self.counts[name]}" == self.checkpoint:
                if self.transient:
                    raise RateLimited(f"checkpoint {self.checkpoint}")
                raise RuntimeError(f"checkpoint {self.checkpoint}")
            return await value(*args, **kwargs)

        return call


class AppliedWriteCrashProxy:
    """Apply the fake Meta mutation, then lose the response."""

    def __init__(self, target, checkpoints):
        self.target = target
        self.checkpoints = set(checkpoints)
        self.counts = {}
        self.applied = []

    def __getattr__(self, name):
        value = getattr(self.target, name)
        if not callable(value):
            return value

        async def call(*args, **kwargs):
            self.counts[name] = self.counts.get(name, 0) + 1
            checkpoint = f"{name}:{self.counts[name]}"
            result = await value(*args, **kwargs)
            if checkpoint in self.checkpoints:
                self.applied.append(checkpoint)
                raise RuntimeError(f"response lost after applied write {checkpoint}")
            return result

        return call


@pytest.mark.parametrize(
    ("kind", "checkpoints"),
    [
        (DecisionKind.PAUSE, {"pause_source:1"}),
        (DecisionKind.SCALE, {"set_budget:1"}),
        (DecisionKind.REALLOCATE, {"set_budget:1"}),
        (DecisionKind.REALLOCATE, {"set_budget:2"}),
        (DecisionKind.REALLOCATE, {"set_budget:2", "set_budget:3"}),
        (DecisionKind.REALLOCATE, {"set_budget:2", "set_budget:4"}),
        (DecisionKind.REPLACE, {"create_paused:1"}),
        (DecisionKind.REPLACE, {"activate_replacement:1", "pause_replacement:1"}),
        (DecisionKind.REPLACE, {"pause_source:1", "pause_replacement:1"}),
    ],
)
@pytest.mark.asyncio
async def test_public_after_applied_write_crash_never_blindly_retries_and_audits_reconciliation(
    engine, kind, checkpoints
):
    if kind is DecisionKind.REPLACE:
        claim = await _public_replace_claim(engine)
        base, builder = ReplacementMeta(), ReplacementBuilder()
    else:
        claim = await _scale_claim(engine, kind)
        base = ReallocationMeta() if kind is DecisionKind.REALLOCATE else BudgetMeta()
        builder = None
    meta = AppliedWriteCrashProxy(base, checkpoints)

    result = await execute_claim(engine, _settings(), meta, builder, claim, NOW)

    assert result.status is ExecutionStatus.RECONCILIATION_REQUIRED
    assert set(meta.applied) == checkpoints
    if kind is DecisionKind.PAUSE:
        assert base.paused is True
    elif kind is DecisionKind.SCALE:
        assert base.budget == 1200
    async with engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT status,audit,failure_category FROM autonomous_actions WHERE id=:id"
                    ),
                    {"id": claim.id},
                )
            )
            .mappings()
            .one()
        )
    assert row["status"] == "reconciliation_required"
    assert row["failure_category"] in {
        "external_state_unproven",
        "reallocation_rollback_unproven",
        "replacement_rollback_unproven",
    }
    if kind in {DecisionKind.REALLOCATE, DecisionKind.REPLACE}:
        assert "rollback_result" in row["audit"]


@pytest.mark.parametrize("stage", ["campaign", "ad_set", "NL", "FR", "EN"])
@pytest.mark.parametrize("steal_lease", [False, True], ids=["reconcile", "lease_lost"])
@pytest.mark.asyncio
async def test_public_real_adapter_applied_activation_stage_crash_compensates_or_fences_stale_worker(
    engine, monkeypatch, stage, steal_lease
):
    claim = await _public_replace_claim(engine)
    settings = _settings(
        meta_app_id="app",
        meta_app_secret="secret",
        meta_system_user_token="token",
        meta_ad_account_id="act",
        meta_page_id="page",
    )
    adapter = MetaExecutionAdapter(settings, object())
    adapter._drafts["50"] = SimpleNamespace(daily_budget_eur=10)
    adapter._identities["50"] = {}
    bundle = {
        "campaign_id": "50",
        "ad_set_id": "60",
        "ad_ids": {"NL": "71", "FR": "72", "EN": "73"},
        "creative_ids": {"NL": "81", "FR": "82", "EN": "83"},
    }

    async def create_paused(**kwargs):
        return bundle

    adapter.create_paused = create_paused
    active = set()
    writes = []

    async def steal():
        if steal_lease:
            async with engine.begin() as conn:
                await conn.execute(
                    text("UPDATE autonomous_actions SET lease_token='stage-stolen' WHERE id=:id"),
                    {"id": claim.id},
                )

    async def replacement_statuses(*args, **kwargs):
        result = {}
        hierarchy = "ACTIVE" if active else "PAUSED"
        for key in ("campaign", "ad_set"):
            result[key] = {"status": hierarchy, "effective_status": hierarchy}
        result["ad_set"]["daily_budget"] = 1000
        for locale in ("NL", "FR", "EN"):
            value = "ACTIVE" if locale in active else "PAUSED"
            result[f"ad:{locale}"] = {"status": value, "effective_status": value}
        return result

    async def activate(*args, **kwargs):
        for item in ("campaign", "ad_set", "NL"):
            active.add(item if item == "NL" else "NL" if False else item)
            writes.append(item)
            if stage == item:
                await steal()
                raise RuntimeError(f"lost response after {item}")
        # Only locale status matters to the adapter's bundle readback.
        active.discard("campaign")
        active.discard("ad_set")
        return {"status": "ACTIVE"}

    async def set_status(config, ad_id, status):
        locale = {"72": "FR", "73": "EN", "31": "source"}[ad_id]
        if locale != "source":
            active.add(locale)
        writes.append(locale)
        if stage == locale:
            await steal()
            raise RuntimeError(f"lost response after {locale}")
        return {"status": status}

    async def pause_bundle(*args, **kwargs):
        writes.append("compensation_pause")
        active.clear()
        return {"paused": True}

    async def source_statuses(config, ids):
        return {
            key: {"status": "ACTIVE", "effective_status": "ACTIVE"}
            for key in ("campaign", "ad_set", "ad")
        }

    async def budget(*args, **kwargs):
        return {
            "ad": {"status": "ACTIVE", "effective_status": "ACTIVE"},
            "ad_set": {"status": "ACTIVE", "effective_status": "ACTIVE", "daily_budget": 1000},
        }

    monkeypatch.setattr(
        "peermarket_agent.autonomy.executor.get_meta_replacement_bundle_statuses",
        replacement_statuses,
    )
    monkeypatch.setattr("peermarket_agent.autonomy.executor.activate_meta_ad", activate)
    monkeypatch.setattr("peermarket_agent.autonomy.executor.set_meta_ad_status", set_status)
    monkeypatch.setattr(
        "peermarket_agent.autonomy.executor.pause_meta_replacement_bundle", pause_bundle
    )
    monkeypatch.setattr("peermarket_agent.autonomy.executor.get_meta_ad_statuses", source_statuses)
    monkeypatch.setattr("peermarket_agent.autonomy.executor.get_meta_budget_state", budget)

    result = await execute_claim(engine, settings, adapter, ReplacementBuilder(), claim, NOW)

    if steal_lease:
        assert result.status is ExecutionStatus.FAILED
        assert result.reason == "lease_lost"
        assert "compensation_pause" not in writes
    else:
        assert result.status is ExecutionStatus.RECONCILIATION_REQUIRED
        assert writes[-1] == "compensation_pause"
        async with engine.connect() as conn:
            audit = await conn.scalar(
                text("SELECT audit FROM autonomous_actions WHERE id=:id"), {"id": claim.id}
            )
        assert audit["rollback_result"]["verified"] is True


@pytest.mark.parametrize(
    ("kind", "checkpoint", "transient"),
    [
        (DecisionKind.PAUSE, "pause_source:1", False),
        (DecisionKind.PAUSE, "read_source:3", True),
        (DecisionKind.SCALE, "set_budget:1", False),
        (DecisionKind.SCALE, "read_source:3", True),
        (DecisionKind.REALLOCATE, "set_budget:1", False),
        (DecisionKind.REALLOCATE, "set_budget:2", False),
        (DecisionKind.REALLOCATE, "read_allocation:5", True),
        (DecisionKind.REALLOCATE, "read_allocation:6", True),
        (DecisionKind.REPLACE, "create_paused:1", False),
        (DecisionKind.REPLACE, "read_replacement:1", True),
        (DecisionKind.REPLACE, "read_replacement:2", True),
        (DecisionKind.REPLACE, "activate_replacement:1", False),
        (DecisionKind.REPLACE, "read_replacement:3", True),
        (DecisionKind.REPLACE, "pause_source:1", False),
        (DecisionKind.REPLACE, "read_source:4", True),
    ],
)
@pytest.mark.asyncio
async def test_public_execute_claim_checkpoint_inventory_reconciles_every_attempted_write(
    engine, kind, checkpoint, transient
):
    if kind is DecisionKind.REPLACE:
        claim = await _public_replace_claim(engine)
        base = ReplacementMeta()
        builder = ReplacementBuilder()
    else:
        claim = await _scale_claim(engine, kind)
        base = ReallocationMeta() if kind is DecisionKind.REALLOCATE else BudgetMeta()
        builder = None
    meta = CheckpointProxy(base, checkpoint, transient=transient)

    result = await execute_claim(engine, _settings(), meta, builder, claim, NOW)

    assert result.status is ExecutionStatus.RECONCILIATION_REQUIRED
    async with engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT status,failure_category,audit FROM autonomous_actions WHERE id=:id"
                    ),
                    {"id": claim.id},
                )
            )
            .mappings()
            .one()
        )
    assert row["status"] == "reconciliation_required"
    assert row["failure_category"] in {
        "external_state_unproven",
        "reallocation_rollback_unproven",
        "replacement_rollback_unproven",
    }


@pytest.mark.parametrize(
    "kind",
    [DecisionKind.PAUSE, DecisionKind.SCALE, DecisionKind.REALLOCATE, DecisionKind.REPLACE],
)
@pytest.mark.asyncio
async def test_public_execute_claim_final_audit_checkpoint_never_stale_finishes(
    engine, monkeypatch, kind
):
    if kind is DecisionKind.REPLACE:
        claim = await _public_replace_claim(engine)
        meta, builder = ReplacementMeta(), ReplacementBuilder()
    else:
        claim = await _scale_claim(engine, kind)
        meta = ReallocationMeta() if kind is DecisionKind.REALLOCATE else BudgetMeta()
        builder = None
    original = finish_action

    async def lose_at_success(db, current, **kwargs):
        if kwargs.get("status") is ActionStatus.SUCCEEDED:
            return False
        return await original(db, current, **kwargs)

    monkeypatch.setattr("peermarket_agent.autonomy.executor.finish_action", lose_at_success)

    result = await execute_claim(engine, _settings(), meta, builder, claim, NOW)

    assert result.status is ExecutionStatus.FAILED
    assert result.reason == "lease_lost"
    async with engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text("SELECT status,after_state FROM autonomous_actions WHERE id=:id"),
                    {"id": claim.id},
                )
            )
            .mappings()
            .one()
        )
    assert row["status"] == "executing"
    assert row["after_state"] == {}
