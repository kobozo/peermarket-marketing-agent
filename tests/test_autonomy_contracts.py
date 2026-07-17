"""Tests for immutable autonomous lifecycle contracts."""

import json
from datetime import UTC, datetime
from datetime import tzinfo as TzInfo

import pytest

from peermarket_agent.autonomy.contracts import (
    ActionStatus,
    DecisionKind,
    FrozenDecision,
    HookExperiment,
    HookVariant,
)


def _hook_variant(variant_id: str = "hook-1", **overrides):
    values = {
        "variant_id": variant_id,
        "experiment_id": "draft-156-hooks-v1",
        "campaign_id": "120249125021520342",
        "ad_set_id": "120249125021520343",
        "landing_page_url": "https://peermarket.eu/signup",
        "fixed_identity": {"audience": "declutterers", "optimization": "LANDING_PAGE_VIEWS"},
        "language_bundles": {
            locale: {"hook": f"{locale} hook", "headline": f"{locale} headline"}
            for locale in ("NL", "FR", "EN")
        },
    }
    values.update(overrides)
    return HookVariant(**values)


def _hook_experiment(**overrides):
    values = {
        "experiment_id": "draft-156-hooks-v1",
        "campaign_id": "120249125021520342",
        "ad_set_id": "120249125021520343",
        "landing_page_url": "https://peermarket.eu/signup",
        "fixed_identity": {"audience": "declutterers", "optimization": "LANDING_PAGE_VIEWS"},
        "variants": tuple(_hook_variant(f"hook-{number}") for number in range(1, 4)),
    }
    values.update(overrides)
    return HookExperiment(**values)


def test_hook_experiment_is_immutable_with_stable_exact_ids():
    experiment = _hook_experiment()
    assert experiment.experiment_id == "draft-156-hooks-v1"
    assert [variant.variant_id for variant in experiment.variants] == ["hook-1", "hook-2", "hook-3"]
    with pytest.raises(AttributeError):
        experiment.campaign_id = "1"
    with pytest.raises(ValueError, match="variant_id"):
        _hook_variant(" hook-1")
    with pytest.raises(ValueError, match="experiment_id"):
        _hook_experiment(experiment_id="draft 156")
    with pytest.raises(ValueError, match="ad_set_id"):
        _hook_variant(ad_set_id="act_123")


def test_hook_experiment_requires_exactly_three_unique_variants():
    with pytest.raises(ValueError, match="exactly three"):
        _hook_experiment(variants=(_hook_variant(),))
    with pytest.raises(ValueError, match="unique"):
        _hook_experiment(variants=tuple(_hook_variant() for _ in range(3)))


def test_hook_variant_requires_complete_nl_fr_en_bundles_and_deep_freezes_them():
    bundles = {locale: {"hook": locale} for locale in ("NL", "FR", "EN")}
    variant = _hook_variant(language_bundles=bundles)
    bundles["NL"]["hook"] = "changed"
    assert variant.language_bundles["NL"]["hook"] == "NL"
    with pytest.raises(TypeError):
        variant.language_bundles["NL"]["hook"] = "changed"
    with pytest.raises(ValueError, match="NL/FR/EN"):
        _hook_variant(language_bundles={"NL": {"hook": "only"}})


def test_hook_experiment_rejects_variant_fixed_identity_drift():
    drifted = _hook_variant("hook-3", fixed_identity={"audience": "other"})
    with pytest.raises(ValueError, match="fixed identity"):
        _hook_experiment(variants=(_hook_variant("hook-1"), _hook_variant("hook-2"), drifted))


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
