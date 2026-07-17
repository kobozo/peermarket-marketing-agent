"""Deterministic evidence and budget policy tests."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from peermarket_agent.autonomy.contracts import DecisionKind
from peermarket_agent.autonomy.policy import evaluate_campaign

NOW = datetime(2026, 7, 17, 12, tzinfo=UTC)


@pytest.fixture
def limits():
    return {
        "performance_snapshot_max_age_hours": 2,
        "learning_min_impressions": 1_000,
        "learning_min_landing_page_views": 30,
        "learning_min_registrations": 10,
        "meta_autonomy_cooldown_hours": 24,
        "meta_autonomy_max_test_days": 7,
        "meta_autonomy_max_replacements_24h": 1,
        "meta_autonomy_max_increase_percent": 20,
        "meta_autonomy_max_daily_budget_eur": 20,
        "meta_no_delivery_grace_hours": 2,
        "meta_account_timezone": "Europe/Brussels",
    }


@pytest.fixture
def qualified_snapshot():
    return _snapshot()


def _variant(variant_id, registrations, *, impressions=1_000, views=30, dimension="hook"):
    return {
        "variant_id": variant_id,
        "publication_id": int(variant_id),
        "channel": "meta",
        "objective": "registrations",
        "language": "nl",
        "audience": "be-founders",
        "creative_dimension": dimension,
        "window_definition": "account_day",
        "impressions": impressions,
        "landing_page_views": views,
        "registrations": registrations,
    }


def _snapshot(**overrides):
    value = {
        "snapshot_id": "snapshot-7",
        "campaign_id": "120249125021520342",
        "captured_at": NOW - timedelta(minutes=30),
        "window_start": NOW - timedelta(days=1),
        "window_end": NOW,
        "complete": True,
        "delivery_state": "healthy",
        "attribution_complete": True,
        "current_budget_cents": 1_000,
        "variants": [_variant("2", 10), _variant("1", 20)],
        "replacement_source": _replacement_source(),
    }
    value.update(overrides)
    return value


def _replacement_source():
    return {
        "draft_id": 7,
        "publication_id": 8,
        "campaign_id": "120249125021520342",
        "experiment_id": "experiment-1",
        "changed_dimension": "hook",
        "locales": {
            locale: {
                "locale": locale,
                "hook": f"{locale} hook",
                "body": f"{locale} body",
                "headline": f"{locale} headline",
                "description": f"{locale} description",
                "cta_label": "Learn More",
            }
            for locale in ("NL", "FR", "EN")
        },
        "audience_profile_key": "declutterers",
        "image_prompt": "real marketplace screenshot",
        "asset_path": "/tmp/source.png",
        "daily_budget_eur": 10,
        "landing_page_url": "https://peermarket.eu/",
        "objective": "OUTCOME_TRAFFIC",
        "current_meta_ids": {
            "campaign_id": "120249125021520342",
            "ad_set_id": "20",
            "ad_ids": {"NL": "31", "FR": "32", "EN": "33"},
            "creative_ids": {"NL": "41", "FR": "42", "EN": "43"},
        },
    }


def _history(*events):
    return tuple(events)


def _event(kind, at, **values):
    return {
        "event_id": values.pop("event_id", f"{kind}-{at.isoformat()}"),
        "kind": kind,
        "at": at,
        **values,
    }


@pytest.mark.parametrize("mutation", ["stale", "partial", "tie", "missing_attribution"])
def test_bad_evidence_always_observes(mutation, qualified_snapshot, limits):
    snapshot = deepcopy(qualified_snapshot)
    if mutation == "stale":
        snapshot["captured_at"] = NOW - timedelta(hours=2, microseconds=1)
    elif mutation == "partial":
        snapshot["complete"] = False
    elif mutation == "tie":
        snapshot["variants"][0]["registrations"] = 20
    else:
        snapshot["attribution_complete"] = False

    assert evaluate_campaign(snapshot, (), limits, NOW).kind is DecisionKind.OBSERVE


def test_exact_evidence_floors_are_eligible(limits):
    decision = evaluate_campaign(_snapshot(), (), limits, NOW)
    assert decision.kind is DecisionKind.REPLACE
    assert decision.evidence["source"] == _replacement_source()


def test_qualified_replacement_without_frozen_source_observes(limits):
    assert (
        evaluate_campaign(_snapshot(replacement_source=None), (), limits, NOW).reason
        == "missing_replacement_source"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [("impressions", 999), ("landing_page_views", 29), ("registrations", 9)],
)
def test_each_variant_must_reach_all_evidence_floors(field, value, limits):
    snapshot = _snapshot()
    snapshot["variants"][0][field] = value
    assert evaluate_campaign(snapshot, (), limits, NOW).kind is DecisionKind.OBSERVE


def test_seven_day_terminal_observation_is_not_directional(limits):
    snapshot = _snapshot(
        window_start=NOW - timedelta(days=7),
        variants=[_variant("1", 3), _variant("2", 2)],
    )
    decision = evaluate_campaign(snapshot, (), limits, NOW)
    assert decision.kind is DecisionKind.OBSERVE
    assert decision.reason == "maximum_test_duration_without_qualified_comparison"


@pytest.mark.parametrize("state", ["no_delivery", "rejected_or_error"])
def test_delivery_failure_observes_with_diagnosis_reason(state, limits):
    extra = {"configured_active_since": NOW - timedelta(hours=2)} if state == "no_delivery" else {}
    decision = evaluate_campaign(_snapshot(delivery_state=state, **extra), (), limits, NOW)
    assert decision.kind is DecisionKind.OBSERVE
    assert decision.reason == f"diagnose_{state}"


def test_variants_must_have_comparable_known_dimensions(limits):
    snapshot = _snapshot()
    snapshot["variants"][0]["audience"] = "other"
    assert evaluate_campaign(snapshot, (), limits, NOW).reason == "not_comparable"

    snapshot = _snapshot()
    snapshot["variants"][0]["unexpected"] = "value"
    assert evaluate_campaign(snapshot, (), limits, NOW).reason == "invalid_snapshot"


def test_cooldown_boundary_is_exclusive(limits):
    recent = _history(_event("replace", NOW - timedelta(hours=24) + timedelta(microseconds=1)))
    boundary = _history(_event("replace", NOW - timedelta(hours=24)))
    assert evaluate_campaign(_snapshot(), recent, limits, NOW).kind is DecisionKind.OBSERVE
    assert evaluate_campaign(_snapshot(), boundary, limits, NOW).kind is DecisionKind.REPLACE


def test_prior_replacement_limit_blocks_another_replacement(limits):
    limits = {**limits, "meta_autonomy_cooldown_hours": 1}
    history = _history(_event("replace", NOW - timedelta(hours=23)))
    assert evaluate_campaign(_snapshot(), history, limits, NOW).reason == "replacement_limit"


def test_winner_with_loser_reallocates_exact_adsets_without_changing_total(limits):
    allocations = {
        "winner": {
            "campaign_id": "120249125021520342",
            "variant_id": "1",
            "ad_set_id": "22",
            "ad_id": "2",
            "old_budget_cents": 400,
            "new_budget_cents": 600,
        },
        "loser": {
            "campaign_id": "120249125021520342",
            "variant_id": "2",
            "ad_set_id": "11",
            "ad_id": "1",
            "old_budget_cents": 600,
            "new_budget_cents": 400,
        },
    }
    snapshot = _snapshot(
        reallocation={
            "old_budget_cents": 1_000,
            "new_budget_cents": 1_000,
            "allocations": allocations,
        }
    )
    decision = evaluate_campaign(snapshot, (), limits, NOW)
    assert decision.kind is DecisionKind.REALLOCATE
    assert decision.old_budget_cents == decision.new_budget_cents == 1_000
    assert decision.allocations == allocations


def test_reallocation_without_exact_two_sided_identity_fails_closed(limits):
    snapshot = _snapshot(reallocation={"old_budget_cents": 1_000, "new_budget_cents": 1_000})
    assert evaluate_campaign(snapshot, (), limits, NOW).reason == "invalid_snapshot"


def test_reallocation_with_non_meta_ids_fails_closed(limits):
    allocations = {
        "winner": {
            "campaign_id": "120249125021520342",
            "variant_id": "1",
            "ad_set_id": "not-meta",
            "ad_id": "2",
            "old_budget_cents": 400,
            "new_budget_cents": 600,
        },
        "loser": {
            "campaign_id": "120249125021520342",
            "variant_id": "2",
            "ad_set_id": "11",
            "ad_id": "1",
            "old_budget_cents": 600,
            "new_budget_cents": 400,
        },
    }
    snapshot = _snapshot(
        reallocation={
            "old_budget_cents": 1000,
            "new_budget_cents": 1000,
            "allocations": allocations,
        }
    )
    assert evaluate_campaign(snapshot, (), limits, NOW).reason == "invalid_snapshot"


def test_scale_is_capped_from_rolling_opening_budget(limits):
    history = _history(
        _event("budget", NOW - timedelta(hours=23), old_budget_cents=1_000, new_budget_cents=1_100),
    )
    decision = evaluate_campaign(
        _snapshot(current_budget_cents=1_100, allow_replacement=False), history, limits, NOW
    )
    assert decision.kind is DecisionKind.SCALE
    assert decision.old_budget_cents == 1_100
    assert decision.new_budget_cents == 1_200


def test_scale_freezes_campaign_total_allocations_and_normalized_policy(limits):
    publications = [
        {
            "publication_id": 3,
            "draft_id": 103,
            "approved_budget_cents": 333,
            "external_ids": {"campaign_id": "120249125021520342", "ad_set_id": "23", "ad_id": "33"},
        },
        {
            "publication_id": 1,
            "draft_id": 101,
            "approved_budget_cents": 1000,
            "external_ids": {"campaign_id": "120249125021520342", "ad_set_id": "21", "ad_id": "31"},
        },
        {
            "publication_id": 2,
            "draft_id": 102,
            "approved_budget_cents": 667,
            "external_ids": {"campaign_id": "120249125021520342", "ad_set_id": "22", "ad_id": "32"},
        },
    ]
    snapshot = _snapshot(
        current_budget_cents=2_000,
        opening_budget_cents=2_000,
        allow_replacement=False,
        frozen_basis={
            "campaign_total_budget_cents": 2_000,
            "campaign_publications": publications,
        },
    )
    decision = evaluate_campaign(
        snapshot, (), {**limits, "meta_autonomy_max_daily_budget_eur": 24}, NOW
    )

    assert decision.kind is DecisionKind.SCALE
    assert (decision.old_budget_cents, decision.new_budget_cents) == (2_000, 2_400)
    assert [item["new_budget_cents"] for item in decision.allocations.values()] == [1200, 800, 400]
    assert sum(item["new_budget_cents"] for item in decision.allocations.values()) == 2_400
    assert decision.evidence["policy_limits"] == {
        "snapshot_age_hours": 2,
        "min_impressions": 1_000,
        "min_landing_page_views": 30,
        "min_registrations": 10,
        "cooldown_hours": 24,
        "max_test_days": 7,
        "max_replacements_24h": 1,
        "max_increase_percent": 20,
        "max_daily_budget_cents": 2_400,
        "no_delivery_grace_hours": 2,
        "complete_window_required": True,
        "account_timezone": "Europe/Brussels",
    }


def test_one_prior_increase_blocks_second_scale(limits):
    limits = {**limits, "meta_autonomy_cooldown_hours": 1}
    history = _history(
        _event("scale", NOW - timedelta(hours=23), old_budget_cents=1_000, new_budget_cents=1_100),
    )
    assert (
        evaluate_campaign(_snapshot(allow_replacement=False), history, limits, NOW).reason
        == "increase_limit"
    )


def test_decrease_does_not_create_scale_headroom(limits):
    history = _history(
        _event("budget", NOW - timedelta(hours=23), old_budget_cents=1_000, new_budget_cents=1_200),
        _event("budget", NOW - timedelta(hours=22), old_budget_cents=1_200, new_budget_cents=900),
    )
    decision = evaluate_campaign(
        _snapshot(current_budget_cents=900, allow_replacement=False), history, limits, NOW
    )
    assert decision.kind is DecisionKind.OBSERVE
    assert decision.reason == "increase_headroom_exhausted"


def test_absolute_eur_twenty_ceiling_is_exact(limits):
    decision = evaluate_campaign(
        _snapshot(current_budget_cents=1_900, opening_budget_cents=1_900, allow_replacement=False),
        (),
        limits,
        NOW,
    )
    assert decision.kind is DecisionKind.SCALE
    assert decision.new_budget_cents == 2_000
    assert (
        evaluate_campaign(
            _snapshot(
                current_budget_cents=2_000, opening_budget_cents=2_000, allow_replacement=False
            ),
            (),
            limits,
            NOW,
        ).kind
        is DecisionKind.OBSERVE
    )


def test_budget_event_history_is_required_for_changed_budget(limits):
    decision = evaluate_campaign(
        _snapshot(current_budget_cents=1_100, allow_replacement=False), (), limits, NOW
    )
    assert decision.kind is DecisionKind.OBSERVE
    assert decision.reason == "missing_budget_history"


def test_decimal_comparison_does_not_round_close_rates_to_a_tie(limits):
    snapshot = _snapshot(
        variants=[
            _variant("1", 10_000_000_000_000_000_000, views=30_000_000_000_000_000_001),
            _variant("2", 9_999_999_999_999_999_999, views=30_000_000_000_000_000_000),
        ]
    )
    decision = evaluate_campaign(snapshot, (), limits, NOW)
    assert decision.kind is DecisionKind.REPLACE
    assert decision.evidence["winner_value"] == str(
        Decimal(10_000_000_000_000_000_000) / Decimal(30_000_000_000_000_000_001)
    )


def test_rate_order_uses_exact_cross_multiplication_beyond_decimal_precision(limits):
    base = 10**40
    snapshot = _snapshot(
        variants=[
            _variant("1", base + 1, views=base),
            _variant("2", base, views=base - 1),
        ]
    )
    decision = evaluate_campaign(snapshot, (), limits, NOW)
    assert decision.kind is DecisionKind.REPLACE
    assert decision.evidence["winner_variant_id"] == "2"
    assert decision.evidence["loser_variant_id"] == "1"


@pytest.mark.parametrize("bad", [True, 1.0, Decimal("NaN"), -1])
def test_invalid_numeric_boundaries_observe(bad, limits):
    snapshot = _snapshot()
    snapshot["variants"][0]["impressions"] = bad
    assert evaluate_campaign(snapshot, (), limits, NOW).reason == "invalid_snapshot"


def test_naive_times_and_history_are_rejected(limits):
    snapshot = _snapshot(captured_at=datetime(2026, 7, 17, 11))
    assert evaluate_campaign(snapshot, (), limits, NOW).reason == "invalid_snapshot"
    history = _history(_event("replace", datetime(2026, 7, 16, 11)))
    assert evaluate_campaign(_snapshot(), history, limits, NOW).reason == "invalid_history"


def test_input_order_does_not_change_decision_or_key(limits):
    snapshot = _snapshot()
    history = _history(
        _event("observe", NOW - timedelta(days=2), event_id="2"),
        _event("observe", NOW - timedelta(days=3), event_id="1"),
    )
    first = evaluate_campaign(snapshot, history, limits, NOW)
    snapshot["variants"].reverse()
    second = evaluate_campaign(snapshot, tuple(reversed(history)), limits, NOW)
    assert first == second


@pytest.mark.parametrize("reverse", [False, True])
def test_duplicate_variant_ids_fail_closed_independent_of_order(limits, reverse):
    variants = [_variant("1", 20), _variant("1", 10)]
    if reverse:
        variants.reverse()
    decision = evaluate_campaign(_snapshot(variants=variants), (), limits, NOW)
    assert decision.kind is DecisionKind.OBSERVE
    assert decision.reason == "invalid_snapshot"


@pytest.mark.parametrize("reverse", [False, True])
def test_duplicate_history_event_ids_fail_closed_independent_of_order(limits, reverse):
    history = [
        _event("observe", NOW - timedelta(days=2), event_id="collision"),
        _event("replace", NOW - timedelta(days=3), event_id="collision"),
    ]
    if reverse:
        history.reverse()
    decision = evaluate_campaign(_snapshot(), tuple(history), limits, NOW)
    assert decision.kind is DecisionKind.OBSERVE
    assert decision.reason == "invalid_history"


class _ExplosiveString:
    def __str__(self):
        raise AssertionError("untrusted identifiers must not be stringified")


def test_malformed_input_is_deterministic_without_a_valid_now(limits):
    malformed = _snapshot(snapshot_id=_ExplosiveString(), variants="bad")
    first = evaluate_campaign(malformed, (), limits, None)
    second = evaluate_campaign(malformed, (), limits, None)
    assert first == second
    assert first.reason == "invalid_snapshot"


@pytest.mark.parametrize("bad_now", [None, datetime(2026, 7, 17, 12), "now"])
def test_normal_evaluation_requires_valid_aware_now(bad_now, limits):
    decision = evaluate_campaign(_snapshot(), (), limits, bad_now)
    assert decision.kind is DecisionKind.OBSERVE
    assert decision.reason == "invalid_snapshot"


@pytest.mark.parametrize(
    ("elapsed", "reason"),
    [
        (timedelta(hours=2) - timedelta(microseconds=1), "no_delivery_grace_period"),
        (timedelta(hours=2), "diagnose_no_delivery"),
        (timedelta(hours=2, microseconds=1), "diagnose_no_delivery"),
    ],
)
def test_no_delivery_requires_explicit_elapsed_active_period(elapsed, reason, limits):
    decision = evaluate_campaign(
        _snapshot(delivery_state="no_delivery", configured_active_since=NOW - elapsed),
        (),
        limits,
        NOW,
    )
    assert decision.kind is DecisionKind.OBSERVE
    assert decision.reason == reason
    assert decision.evidence["configured_active_since"] == (NOW - elapsed).isoformat()


def test_no_delivery_without_active_since_fails_closed(limits):
    decision = evaluate_campaign(_snapshot(delivery_state="no_delivery"), (), limits, NOW)
    assert decision.reason == "invalid_snapshot"


def test_future_history_is_rejected(limits):
    history = (_event("observe", NOW + timedelta(microseconds=1)),)
    decision = evaluate_campaign(_snapshot(), history, limits, NOW)
    assert decision.kind is DecisionKind.OBSERVE
    assert decision.reason == "invalid_history"


@pytest.mark.parametrize("field", ["snapshot_id"])
@pytest.mark.parametrize("bad", ["", "   ", 7, _ExplosiveString()])
def test_snapshot_stable_id_must_be_a_nonempty_string(field, bad, limits):
    decision = evaluate_campaign(_snapshot(**{field: bad}), (), limits, NOW)
    assert decision.reason == "invalid_snapshot"


@pytest.mark.parametrize("bad", ["", "   ", 7, _ExplosiveString()])
def test_history_stable_id_must_be_a_nonempty_string(bad, limits):
    decision = evaluate_campaign(
        _snapshot(), (_event("observe", NOW - timedelta(days=2), event_id=bad),), limits, NOW
    )
    assert decision.reason == "invalid_history"
