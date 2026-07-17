"""Execution safety contracts for autonomous Meta actions."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from peermarket_agent.autonomy.executor import ExecutionStatus, execute_claim

NOW = datetime(2026, 7, 17, 12, tzinfo=UTC)


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
