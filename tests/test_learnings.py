from datetime import date

from peermarket_agent.learnings import (
    DEFAULT_THRESHOLDS,
    EvidenceVariant,
    eligible_learning,
)


def variant(**overrides):
    values = {
        "evidence_id": "publication:1:2026-07-15",
        "publication_id": 1,
        "channel": "meta",
        "objective": "OUTCOME_TRAFFIC",
        "language": "NL",
        "audience": "declutterers",
        "window_definition": "utc-day",
        "window_start": date(2026, 7, 15),
        "window_stop": date(2026, 7, 16),
        "impressions": 1_000,
        "landing_page_views": 30,
        "registrations": 10,
        "metric_values": {
            "impressions": 1_000,
            "meta_landing_page_views": 30,
            "registrations": 10,
        },
    }
    values.update(overrides)
    return EvidenceVariant(**values)


def test_single_ad_never_creates_reusable_learning():
    decision = eligible_learning([variant()], DEFAULT_THRESHOLDS)
    assert decision.eligible is False
    assert decision.reason == "requires_comparable_variants"


def test_conversion_learning_requires_ten_registrations_each():
    decision = eligible_learning(
        [variant(), variant(evidence_id="publication:2:2026-07-15", registrations=9)],
        DEFAULT_THRESHOLDS,
    )
    assert decision.eligible is False
    assert decision.reason == "insufficient_conversion_evidence"


def test_delivery_thresholds_are_exact_and_apply_to_each_variant():
    qualified = variant()
    low_impressions = variant(evidence_id="two", impressions=999)
    low_lpv = variant(evidence_id="three", landing_page_views=29)

    assert eligible_learning([qualified, low_impressions], DEFAULT_THRESHOLDS).reason == (
        "insufficient_delivery_evidence"
    )
    assert eligible_learning([qualified, low_lpv], DEFAULT_THRESHOLDS).reason == (
        "insufficient_delivery_evidence"
    )


def test_variants_must_match_every_comparison_dimension():
    first = variant()
    for field, value in (
        ("channel", "tiktok"),
        ("objective", "OUTCOME_SALES"),
        ("language", "FR"),
        ("audience", "trust_conscious_locals"),
        ("window_definition", "rolling-24h"),
        ("window_start", date(2026, 7, 14)),
        ("window_stop", date(2026, 7, 17)),
    ):
        second = variant(evidence_id=f"different-{field}", **{field: value})
        assert eligible_learning([first, second], DEFAULT_THRESHOLDS).reason == "not_comparable"


def test_missing_comparison_dimensions_are_never_defaulted():
    for field in (
        "channel",
        "objective",
        "language",
        "audience",
        "window_definition",
        "window_start",
        "window_stop",
    ):
        first = variant(**{field: None})
        second = variant(
            evidence_id=f"publication:2:missing-{field}",
            publication_id=2,
            **{field: None},
        )
        decision = eligible_learning([first, second], DEFAULT_THRESHOLDS)
        assert decision.eligible is False
        assert decision.reason == "missing_comparison_dimensions"


def test_blank_comparison_dimensions_are_missing():
    for field in ("channel", "objective", "language", "audience", "window_definition"):
        decision = eligible_learning(
            [
                variant(**{field: " "}),
                variant(
                    evidence_id=f"publication:2:blank-{field}",
                    publication_id=2,
                    **{field: " "},
                ),
            ],
            DEFAULT_THRESHOLDS,
        )
        assert decision.reason == "missing_comparison_dimensions"


def test_two_distinct_qualified_variants_are_eligible_with_evidence():
    decision = eligible_learning(
        [
            variant(),
            variant(evidence_id="publication:2:2026-07-15", publication_id=2),
        ],
        DEFAULT_THRESHOLDS,
    )

    assert decision.eligible is True
    assert decision.reason == "thresholds_met"
    assert decision.evidence_ids == (
        "publication:1:2026-07-15",
        "publication:2:2026-07-15",
    )
    assert decision.sample == {
        "variants": 2,
        "impressions": 2_000,
        "landing_page_views": 60,
        "registrations": 20,
    }


def test_duplicate_evidence_is_not_two_true_variants():
    assert eligible_learning([variant(), variant()], DEFAULT_THRESHOLDS).reason == (
        "requires_comparable_variants"
    )
