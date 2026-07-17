"""Tests for immutable autonomous lifecycle contracts."""

import json
from datetime import UTC, datetime
from datetime import tzinfo as TzInfo

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


def test_reallocation_requires_exact_winner_and_loser_budget_intent():
    with pytest.raises(ValueError, match="allocations"):
        _decision(
            kind=DecisionKind.REALLOCATE,
            old_budget_cents=1000,
            new_budget_cents=1000,
        )


def test_frozen_decision_deeply_isolates_and_freezes_evidence():
    source = {
        "metrics": [{"name": "spend", "values": [12, 24]}],
        "labels": {"winner", "stable"},
    }
    decision = _decision(evidence=source)

    source["metrics"][0]["values"].append(48)
    source["labels"].add("mutated")

    assert decision.evidence["metrics"][0]["values"] == [12, 24]
    assert set(decision.evidence["labels"]) == {"winner", "stable"}

    with pytest.raises(TypeError):
        decision.evidence["new"] = True
    with pytest.raises(TypeError):
        decision.evidence["metrics"][0]["values"].append(48)
    with pytest.raises(TypeError):
        decision.evidence["metrics"][0]["name"] = "clicks"


def test_frozen_decision_evidence_remains_json_serializable():
    decision = _decision(evidence={"metrics": [1, {"valid": True}], "labels": {"a", "b"}})

    assert json.loads(json.dumps(decision.evidence)) == {
        "metrics": [1, {"valid": True}],
        "labels": ["a", "b"],
    }


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


def test_frozen_decision_rejects_tzinfo_with_no_utc_offset():
    class MissingOffset(TzInfo):
        def utcoffset(self, dt):
            return None

        def dst(self, dt):
            return None

    with pytest.raises(ValueError, match="timezone-aware"):
        _decision(window_start=datetime(2026, 7, 15, tzinfo=MissingOffset()))


@pytest.mark.parametrize(
    ("field", "value"),
    [("evidence", {}), ("reason", ""), ("idempotency_key", "")],
)
def test_frozen_decision_requires_audit_inputs(field, value):
    with pytest.raises(ValueError, match=field):
        _decision(**{field: value})
