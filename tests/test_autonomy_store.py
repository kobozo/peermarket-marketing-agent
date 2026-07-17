"""PostgreSQL contracts for the autonomous action store."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from peermarket_agent.autonomy.contracts import (
    ActionStatus,
    DecisionKind,
    FrozenDecision,
    HookExperiment,
    HookVariant,
)
from peermarket_agent.autonomy.store import (
    ClaimedAction,
    begin_execution,
    block_campaign_for_reconciliation,
    campaign_history,
    claim_next_action,
    enqueue_action,
    finish_action,
    list_experiment_variants,
    record_budget_event,
    record_decision,
    record_experiment,
    record_experiment_variant,
    release_action,
    require_reconciliation,
)
from peermarket_agent.db.migrations import run_migrations


@pytest.fixture
async def engine():
    engine = create_async_engine(os.environ["AGENT_DB_URL"], future=True)
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await run_migrations(engine)
    yield engine
    await engine.dispose()


def decision(
    key: str = "decision-1", campaign_id: str = "123", *, kind: DecisionKind = DecisionKind.SCALE
) -> FrozenDecision:
    return FrozenDecision(
        kind=kind,
        campaign_id=campaign_id,
        evidence={"snapshot_id": key, "rate": "0.125"},
        reason="proven winner",
        window_start=datetime(2026, 7, 16, tzinfo=UTC),
        window_end=datetime(2026, 7, 17, tzinfo=UTC),
        idempotency_key=key,
        old_budget_cents=1000 if kind in {DecisionKind.SCALE, DecisionKind.REALLOCATE} else None,
        new_budget_cents=1200 if kind in {DecisionKind.SCALE, DecisionKind.REALLOCATE} else None,
    )


def hook_experiment() -> HookExperiment:
    identity = {
        "audience": "declutterers",
        "optimization": "LANDING_PAGE_VIEWS",
        "format": "single_image",
        "visual": "asset-1",
        "delivery": "lowest_cost",
    }
    variants = tuple(
        HookVariant(
            variant_id=f"hook-{number}",
            experiment_id="draft-156-hooks-v1",
            campaign_id="120249125021520342",
            ad_set_id="120249125021520343",
            landing_page_url="https://peermarket.eu/signup",
            changed_dimension="hook",
            fixed_identity=identity,
            language_bundles={
                locale: {
                    "hook": f"{locale} hook {number}",
                    "body": f"{locale} body",
                    "headline": f"{locale} headline",
                    "description": f"{locale} description",
                    "cta_label": "Learn More",
                }
                for locale in ("NL", "FR", "EN")
            },
        )
        for number in range(1, 4)
    )
    return HookExperiment(
        experiment_id="draft-156-hooks-v1",
        campaign_id="120249125021520342",
        ad_set_id="120249125021520343",
        landing_page_url="https://peermarket.eu/signup",
        changed_dimension="hook",
        fixed_identity=identity,
        variants=variants,
    )


async def test_record_experiment_persists_exact_nine_identity_rows_idempotently(engine):
    experiment = hook_experiment()
    first = await record_experiment(engine, experiment)
    second = await record_experiment(engine, experiment)
    listed = await list_experiment_variants(engine, experiment.experiment_id)
    assert len(first) == len(second) == len(listed) == 9
    assert [(row.variant_id, row.language) for row in listed] == [
        (f"hook-{number}", locale) for number in range(1, 4) for locale in ("NL", "FR", "EN")
    ]
    assert all(row.fixed_identity == experiment.fixed_identity for row in listed)


async def test_partial_experiment_bundle_recovers_without_duplicate_rows(engine):
    experiment = hook_experiment()
    await record_experiment_variant(engine, experiment, experiment.variants[0], "NL")
    assert len(await list_experiment_variants(engine, experiment.experiment_id)) == 1
    recovered = await record_experiment(engine, experiment)
    assert len(recovered) == 9


async def test_experiment_identity_drift_is_rejected_instead_of_overwritten(engine):
    experiment = hook_experiment()
    await record_experiment_variant(engine, experiment, experiment.variants[0], "NL")
    drifted = hook_experiment()
    object.__setattr__(drifted, "landing_page_url", "https://peermarket.eu/other")
    with pytest.raises(ValueError, match="identity drift"):
        await record_experiment_variant(engine, drifted, drifted.variants[0], "FR")


async def test_record_decision_is_idempotent_and_never_overwrites_evidence(engine):
    original = decision()
    first = await record_decision(engine, original)
    conflicting = FrozenDecision(
        kind=DecisionKind.OBSERVE,
        campaign_id="999",
        evidence={"snapshot_id": "different"},
        reason="different",
        window_start=original.window_start,
        window_end=original.window_end,
        idempotency_key=original.idempotency_key,
    )
    second = await record_decision(engine, conflicting)

    assert first.created is True
    assert second.created is False
    assert second.id == first.id
    async with engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text("SELECT campaign_id, evidence FROM autonomous_decisions WHERE id=:id"),
                    {"id": first.id},
                )
            )
            .mappings()
            .one()
        )
    assert row == {"campaign_id": "123", "evidence": {"snapshot_id": "decision-1", "rate": "0.125"}}


async def test_concurrent_enqueue_serializes_one_nonterminal_action_per_campaign(engine):
    item = decision()
    first, second = await asyncio.gather(enqueue_action(engine, item), enqueue_action(engine, item))
    assert sorted([first.created, second.created]) == [False, True]
    assert first.id == second.id
    async with engine.connect() as conn:
        count = await conn.scalar(
            text(
                "SELECT count(*) FROM autonomous_actions WHERE campaign_id=:campaign_id "
                "AND status IN ('pending','leased','executing')"
            ),
            {"campaign_id": item.campaign_id},
        )
    assert count == 1


async def test_claims_use_skip_locked_across_simultaneous_connections(engine):
    await enqueue_action(engine, decision("one", "101"))
    await enqueue_action(engine, decision("two", "202"))
    first, second = await asyncio.gather(
        claim_next_action(engine, "worker-a", lease_seconds=60),
        claim_next_action(engine, "worker-b", lease_seconds=60),
    )
    assert isinstance(first, ClaimedAction)
    assert isinstance(second, ClaimedAction)
    assert first.id != second.id
    assert {first.lease_owner, second.lease_owner} == {"worker-a", "worker-b"}
    assert first.lease_token != second.lease_token


async def test_claim_skips_a_row_locked_by_an_open_transaction(engine):
    first = await enqueue_action(engine, decision("locked", "101"))
    second = await enqueue_action(engine, decision("available", "202"))
    async with engine.connect() as locking_conn:
        transaction = await locking_conn.begin()
        await locking_conn.execute(
            text("SELECT id FROM autonomous_actions WHERE id=:id FOR UPDATE"), {"id": first.id}
        )
        try:
            claimed = await asyncio.wait_for(
                claim_next_action(engine, "worker", lease_seconds=60), timeout=1
            )
        finally:
            await transaction.rollback()

    assert claimed is not None
    assert claimed.id == second.id


async def test_claim_skips_locked_expired_execution_during_reconciliation(engine):
    expired = await enqueue_action(engine, decision("expired-execution", "101"))
    stale = await claim_next_action(engine, "crashed-worker", lease_seconds=60)
    assert stale is not None and stale.id == expired.id
    assert await begin_execution(engine, stale)
    available = await enqueue_action(engine, decision("available", "202"))
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE autonomous_actions SET lease_expires_at=NOW()-INTERVAL '1 second' "
                "WHERE id=:id"
            ),
            {"id": expired.id},
        )

    claimant_engine = create_async_engine(
        os.environ["AGENT_DB_URL"],
        future=True,
        connect_args={"server_settings": {"statement_timeout": "500"}},
    )
    async with engine.connect() as locking_conn:
        transaction = await locking_conn.begin()
        await locking_conn.execute(
            text("SELECT id FROM autonomous_actions WHERE id=:id FOR UPDATE"), {"id": expired.id}
        )
        try:
            claimed = await claim_next_action(claimant_engine, "replacement", lease_seconds=60)
        finally:
            await transaction.rollback()
            await claimant_engine.dispose()

    assert claimed is not None
    assert claimed.id == available.id


async def test_expired_lease_is_recovered_with_a_new_token(engine):
    await enqueue_action(engine, decision())
    stale = await claim_next_action(engine, "old", lease_seconds=60)
    assert stale is not None
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE autonomous_actions SET lease_expires_at=NOW()-INTERVAL '1 second' WHERE id=:id"
            ),
            {"id": stale.id},
        )
    reclaimed = await claim_next_action(engine, "new", lease_seconds=60)
    assert reclaimed is not None
    assert reclaimed.id == stale.id
    assert reclaimed.lease_token != stale.lease_token
    assert await begin_execution(engine, stale) is False
    assert await begin_execution(engine, reclaimed) is True


async def test_worker_crash_during_execution_is_reclaimed_only_for_reconciliation(engine):
    queued = await enqueue_action(engine, decision())
    stale = await claim_next_action(engine, "crashed-worker", lease_seconds=60)
    assert stale is not None and await begin_execution(engine, stale)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE autonomous_actions SET lease_expires_at=NOW()-INTERVAL '1 second' WHERE id=:id"
            ),
            {"id": stale.id},
        )

    reclaimed = await claim_next_action(engine, "replacement", lease_seconds=60)
    assert reclaimed is not None
    assert reclaimed.id == stale.id
    assert reclaimed.lease_token != stale.lease_token
    assert await begin_execution(engine, reclaimed)
    row = (await campaign_history(engine, "123"))[0]
    assert row["id"] == queued.id
    assert row["status"] == "executing"
    assert row["failure_category"] == "worker_crash_during_execution"
    assert "crashed-worker" not in (row["failure_message"] or "")
    assert (await enqueue_action(engine, decision("later"))).created is False


async def test_claim_roundtrips_the_complete_frozen_decision(engine):
    original = decision()
    await enqueue_action(engine, original)
    claim = await claim_next_action(engine, "worker")

    assert claim is not None
    assert claim.decision == original
    with pytest.raises(TypeError):
        claim.decision.evidence["snapshot_id"] = "changed"


async def test_transitions_reject_wrong_token_and_unexpected_status(engine):
    await enqueue_action(engine, decision())
    claim = await claim_next_action(engine, "worker", lease_seconds=60)
    assert claim is not None
    impostor = ClaimedAction(
        id=claim.id,
        decision_id=claim.decision_id,
        campaign_id=claim.campaign_id,
        kind=claim.kind,
        lease_owner=claim.lease_owner,
        lease_token="wrong",
        lease_expires_at=claim.lease_expires_at,
        decision=claim.decision,
    )
    assert await begin_execution(engine, impostor) is False
    assert await finish_action(engine, claim, status=ActionStatus.SUCCEEDED) is False
    assert await begin_execution(engine, claim) is True
    assert await begin_execution(engine, claim) is False


async def test_stale_worker_cannot_mark_reconciliation_after_takeover(engine):
    await enqueue_action(engine, decision())
    stale = await claim_next_action(engine, "old", lease_seconds=60)
    assert stale is not None
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE autonomous_actions SET lease_expires_at=NOW()-INTERVAL '1 second' WHERE id=:id"
            ),
            {"id": stale.id},
        )
    current = await claim_next_action(engine, "new", lease_seconds=60)
    assert current is not None and current.lease_token != stale.lease_token
    assert not await require_reconciliation(
        engine, stale, failure_category="stale-worker", failure_message="must not persist"
    )
    async with engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT status, failure_category, lease_token FROM autonomous_actions WHERE id=:id"
                    ),
                    {"id": stale.id},
                )
            )
            .mappings()
            .one()
        )
    assert row == {"status": "leased", "failure_category": None, "lease_token": current.lease_token}


async def test_successful_finish_writes_audit_and_budget_atomically(engine):
    await enqueue_action(engine, decision())
    claim = await claim_next_action(engine, "worker", lease_seconds=60)
    assert claim is not None and await begin_execution(engine, claim)
    finished = await finish_action(
        engine,
        claim,
        status=ActionStatus.SUCCEEDED,
        before_state={"budget": Decimal("10.00")},
        after_state={"budget": Decimal("12.00")},
        rollback_result={"attempted": False},
        next_evaluation_at=datetime.now(UTC) + timedelta(hours=24),
        budget=(1000, 1200),
    )
    assert finished is True
    history = await campaign_history(engine, "123")
    assert history[0]["status"] == "succeeded"
    assert history[0]["before_state"] == {"budget": "10.00"}
    assert history[0]["after_state"] == {"budget": "12.00"}
    assert history[0]["audit"]["rollback_result"] == {"attempted": False}
    assert history[0]["budget_events"][0]["amount_cents"] == 200


async def test_successful_finish_uses_locked_action_campaign_for_budget_event(engine):
    await enqueue_action(engine, decision())
    claim = await claim_next_action(engine, "worker")
    assert claim is not None and await begin_execution(engine, claim)
    forged = ClaimedAction(
        id=claim.id,
        decision_id=claim.decision_id,
        campaign_id="999",
        kind=claim.kind,
        lease_owner=claim.lease_owner,
        lease_token=claim.lease_token,
        lease_expires_at=claim.lease_expires_at,
        decision=claim.decision,
    )

    assert await finish_action(engine, forged, status=ActionStatus.SUCCEEDED, budget=(1000, 1200))
    history = await campaign_history(engine, "123")
    assert history[0]["budget_events"][0]["campaign_id"] == "123"
    assert await campaign_history(engine, "999") == []


async def test_failure_is_sanitized_and_release_returns_to_pending(engine):
    await enqueue_action(engine, decision())
    claim = await claim_next_action(engine, "worker", lease_seconds=60)
    assert claim is not None
    assert await release_action(
        engine, claim, failure_category=" rate limit!! ", failure_message=" token=secret\nretry "
    )
    row = (await campaign_history(engine, "123"))[0]
    assert row["status"] == "pending"
    assert row["failure_category"] == "rate_limit"
    assert "secret" not in row["failure_message"]


@pytest.mark.parametrize(
    "message,secrets",
    [
        ("Authorization: Bearer auth-secret", ["auth-secret"]),
        ("Authorization: Basic dXNlcjpwYXNz", ["dXNlcjpwYXNz"]),
        (
            'Authorization = Digest username="admin", response="digest-secret"',
            ["admin", "digest-secret"],
        ),
        ("Authorization: Custom-Scheme custom-secret", ["custom-secret"]),
        ('{"Authorization":"Basic json-ish-secret"}', ["json-ish-secret"]),
        ("Authorization: 'Custom quoted secret'", ["Custom quoted secret"]),
        ("request failed Bearer bare-secret", ["bare-secret"]),
        ('{"access_token":"json-secret","token": "json-token"}', ["json-secret", "json-token"]),
        (
            "https://example.test/?access_token=query-secret&appsecret_proof=proof-secret",
            ["query-secret", "proof-secret"],
        ),
        (
            "token='quoted secret' password=pass-secret secret: top-secret",
            ["quoted secret", "pass-secret", "top-secret"],
        ),
    ],
)
async def test_failure_sanitizer_redacts_adversarial_credentials(engine, message, secrets):
    await enqueue_action(engine, decision())
    claim = await claim_next_action(engine, "worker")
    assert claim is not None
    assert await release_action(engine, claim, failure_category=message, failure_message=message)
    row = (await campaign_history(engine, "123"))[0]
    persisted = f"{row['failure_category']} {row['failure_message']}"
    assert "redacted" in persisted
    for secret in secrets:
        assert secret not in persisted


async def test_audit_state_never_persists_raw_credentials(engine):
    await enqueue_action(engine, decision())
    claim = await claim_next_action(engine, "worker")
    assert claim is not None and await begin_execution(engine, claim)
    assert await finish_action(
        engine,
        claim,
        status=ActionStatus.FAILED,
        before_state={"Authorization": "Bearer state-secret"},
        rollback_result={"url": "https://x.test?access_token=audit-secret"},
    )
    row = (await campaign_history(engine, "123"))[0]
    persisted = str(row["before_state"]) + str(row["audit"])
    assert "state-secret" not in persisted
    assert "audit-secret" not in persisted


async def test_reconciliation_block_is_terminal_and_prevents_new_enqueue(engine):
    await enqueue_action(engine, decision())
    claim = await claim_next_action(engine, "worker", lease_seconds=60)
    assert claim is not None and await begin_execution(engine, claim)
    assert await block_campaign_for_reconciliation(
        engine,
        claim,
        before_state={"budget": 1000},
        after_state={"budget": 1200},
        failure_category="verification_mismatch",
        failure_message="Meta state differed",
    )
    blocked = await enqueue_action(engine, decision("later", "123"))
    assert blocked.created is False
    assert blocked.status is ActionStatus.RECONCILIATION_REQUIRED


async def test_record_budget_event_is_append_only_history(engine):
    queued = await enqueue_action(engine, decision())
    first = await record_budget_event(engine, queued.id, "123", 1000, 900)
    second = await record_budget_event(engine, queued.id, "123", 900, 1100)
    assert first.amount_cents == -100
    assert second.amount_cents == 200
    history = await campaign_history(engine, "123")
    assert [event["amount_cents"] for event in history[0]["budget_events"]] == [-100, 200]


async def test_record_budget_event_rejects_mismatched_action_campaign(engine):
    queued = await enqueue_action(engine, decision())
    with pytest.raises(ValueError, match="campaign"):
        await record_budget_event(engine, queued.id, "999", 1000, 1200)


async def test_budget_events_reject_update_and_delete_at_database(engine):
    queued = await enqueue_action(engine, decision())
    event = await record_budget_event(engine, queued.id, "123", 1000, 1200)
    for statement in (
        "UPDATE autonomous_budget_events SET amount_cents=0 WHERE id=:id",
        "DELETE FROM autonomous_budget_events WHERE id=:id",
    ):
        with pytest.raises(Exception, match="append-only"):
            async with engine.begin() as conn:
                await conn.execute(text(statement), {"id": event.id})
