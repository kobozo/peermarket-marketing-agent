"""Task 7 autonomous lifecycle orchestration contracts."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from peermarket_agent.agent.loops.autonomy import run_autonomy_cycle
from peermarket_agent.autonomy.contracts import DecisionKind, FrozenDecision

NOW = datetime(2026, 7, 17, 12, tzinfo=UTC)


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
