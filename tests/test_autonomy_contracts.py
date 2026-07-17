"""Tests for immutable autonomous lifecycle contracts."""

from datetime import UTC, datetime

import pytest

from peermarket_agent.autonomy.contracts import ActionStatus, DecisionKind, FrozenDecision


def _decision(**overrides):
    values = {
        "kind": DecisionKind.OBSERVE,
        "campaign_id": "120249125021520342",
        "window_start": datetime(2026, 7, 15, tzinfo=UTC),
        "window_end": datetime(2026, 7, 16, tzinfo=UTC),
        "evidence": {"snapshot_id": 42},
        "reason": "insufficient evidence",
        "idempotency_key": "campaign:120249125021520342:2026-07-16",
    }
    values.update(overrides)
    return FrozenDecision(**values)


def test_decision_and_action_enums_are_string_contracts():
    assert {kind.value for kind in DecisionKind} == {
        "observe",
        "pause",
        "replace",
        "reallocate",
        "scale",
    }
    assert {status.value for status in ActionStatus} == {
        "pending",
        "leased",
        "executing",
        "succeeded",
        "failed",
        "cancelled",
        "reconciliation_required",
    }


def test_frozen_decision_is_immutable():
    decision = _decision()

    with pytest.raises(AttributeError):
        decision.reason = "changed"


def test_frozen_decision_rejects_scale_without_budget_values():
    with pytest.raises(ValueError):
        FrozenDecision(
            kind=DecisionKind.SCALE,
            campaign_id="1",
            evidence={},
            reason="winner",
        )


@pytest.mark.parametrize("kind", [DecisionKind.REALLOCATE, DecisionKind.SCALE])
def test_frozen_decision_rejects_budget_action_without_positive_budget_values(kind):
    with pytest.raises(ValueError, match="budget"):
        _decision(kind=kind)

    with pytest.raises(ValueError, match="budget"):
        _decision(kind=kind, old_budget_cents=1000, new_budget_cents=0)


@pytest.mark.parametrize("campaign_id", ["", " 123", "123 ", "act_123", "12a", "١٢٣"])
def test_frozen_decision_requires_exact_numeric_campaign_id(campaign_id):
    with pytest.raises(ValueError, match="campaign_id"):
        _decision(campaign_id=campaign_id)


def test_frozen_decision_requires_timezone_aware_ordered_window():
    with pytest.raises(ValueError, match="timezone-aware"):
        _decision(window_start=datetime(2026, 7, 15))

    with pytest.raises(ValueError, match="window"):
        _decision(window_start=datetime(2026, 7, 17, tzinfo=UTC))


@pytest.mark.parametrize(
    ("field", "value"),
    [("evidence", {}), ("reason", ""), ("idempotency_key", "")],
)
def test_frozen_decision_requires_audit_inputs(field, value):
    with pytest.raises(ValueError, match=field):
        _decision(**{field: value})
