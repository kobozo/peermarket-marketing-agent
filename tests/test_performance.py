"""Deterministic performance derivation and Meta delivery classification."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from peermarket_agent.performance import classify_delivery, derive_performance

NOW = datetime(2026, 7, 16, 12, tzinfo=UTC)
ACTIVE = {
    "campaign": {"status": "ACTIVE", "effective_status": "ACTIVE"},
    "ad_set": {"status": "ACTIVE", "effective_status": "ACTIVE"},
    "ad": {"status": "ACTIVE", "effective_status": "ACTIVE"},
}


def test_meta_restatement_never_creates_negative_delta():
    result = derive_performance(
        {"spend_cents": 300, "impressions": 1000},
        {"spend_cents": 280, "impressions": 990},
    )

    assert result["delta"] == {"spend_cents": 0, "impressions": 0}
    assert result["restated"] is True


def test_first_snapshot_uses_zero_baseline():
    current = {"spend_cents": 300, "impressions": 1000, "ctr": 1.2}

    result = derive_performance(None, current)

    assert result == {
        "latest": current,
        "previous": {},
        "delta": {"spend_cents": 300, "impressions": 1000},
        "restated": False,
    }


def test_active_zero_impressions_after_grace_is_no_delivery():
    assert (
        classify_delivery(ACTIVE, {"impressions": 0}, NOW - timedelta(hours=3), NOW, 2)
        == "no_delivery"
    )


def test_active_zero_impressions_during_grace_is_unknown():
    assert (
        classify_delivery(ACTIVE, {"impressions": 0}, NOW - timedelta(hours=1), NOW, 2) == "unknown"
    )


def test_active_impressions_is_healthy():
    assert (
        classify_delivery(ACTIVE, {"impressions": 1}, NOW - timedelta(hours=3), NOW, 2) == "healthy"
    )


@pytest.mark.parametrize("state", ["IN_PROCESS", "PENDING_REVIEW", "PREAPPROVED"])
def test_documented_review_states_are_reviewing(state):
    statuses = {**ACTIVE, "ad": {"status": "ACTIVE", "effective_status": state}}

    assert classify_delivery(statuses, {}, NOW, NOW, 2) == "reviewing"


@pytest.mark.parametrize("state", ["ARCHIVED", "DELETED"])
def test_documented_terminal_states_are_terminal(state):
    statuses = {"ad": {"status": state, "effective_status": state}}

    assert classify_delivery(statuses, {}, NOW, NOW, 2) == "terminal"


@pytest.mark.parametrize("state", ["DISAPPROVED", "WITH_ISSUES", "ERROR"])
def test_documented_error_states_are_rejected_or_error(state):
    statuses = {"ad": {"status": "ACTIVE", "effective_status": state}}

    assert classify_delivery(statuses, {}, NOW, NOW, 2) == "rejected_or_error"


def test_reported_issues_are_rejected_or_error():
    statuses = {"ad": {"status": "ACTIVE", "effective_status": "ACTIVE", "issues": ["x"]}}

    assert classify_delivery(statuses, {}, NOW, NOW, 2) == "rejected_or_error"


def test_unknown_or_unavailable_status_is_unknown():
    assert classify_delivery({}, {"impressions": 20}, NOW, NOW, 2) == "unknown"


def test_grace_comparison_handles_different_aware_timezones():
    cet = timezone(timedelta(hours=2))
    published_at = datetime(2026, 7, 16, 11, tzinfo=cet)

    assert classify_delivery(ACTIVE, {}, published_at, NOW, 2) == "no_delivery"


@pytest.mark.parametrize("field", ["published_at", "now"])
def test_grace_comparison_rejects_naive_datetimes(field):
    values = {"published_at": NOW, "now": NOW}
    values[field] = datetime(2026, 7, 16, 12)

    with pytest.raises(ValueError, match="timezone-aware"):
        classify_delivery(ACTIVE, {}, values["published_at"], values["now"], 2)
