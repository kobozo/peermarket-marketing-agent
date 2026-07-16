"""Pure performance derivation and Meta delivery classification."""

from datetime import datetime, timedelta
from numbers import Real

_NUMERIC_METRICS = (
    "spend_cents",
    "impressions",
    "reach",
    "clicks",
    "inline_link_clicks",
    "outbound_clicks",
    "landing_page_views",
)
_REVIEW_STATES = {"IN_PROCESS", "PENDING_REVIEW", "PREAPPROVED", "PENDING_BILLING_INFO"}
_TERMINAL_STATES = {"ARCHIVED", "DELETED"}
_ERROR_STATES = {"DISAPPROVED", "WITH_ISSUES", "ERROR"}


def derive_performance(previous: dict | None, current: dict) -> dict:
    """Derive non-negative deltas while retaining evidence of Meta restatements."""
    previous = previous or {}
    present_metrics = tuple(metric for metric in _NUMERIC_METRICS if metric in current)
    restated = any(
        _number(current.get(metric)) < _number(previous.get(metric)) for metric in present_metrics
    )
    delta = {
        metric: max(0, _number(current.get(metric)) - _number(previous.get(metric)))
        for metric in present_metrics
    }
    return {
        "latest": current,
        "previous": previous,
        "delta": delta,
        "restated": restated,
    }


def classify_delivery(
    statuses: dict,
    snapshot: dict,
    published_at: datetime,
    now: datetime,
    grace_hours: float,
) -> str:
    """Classify a Meta hierarchy using documented effective delivery states."""
    resources = [value for value in (statuses or {}).values() if isinstance(value, dict)]
    if not resources:
        return "unknown"

    observed_states = {
        str(resource.get(field, "")).upper()
        for resource in resources
        for field in ("status", "effective_status")
        if resource.get(field)
    }
    if any(resource.get("issues") for resource in resources) or observed_states & _ERROR_STATES:
        return "rejected_or_error"
    if observed_states & _TERMINAL_STATES:
        return "terminal"
    if observed_states & _REVIEW_STATES:
        return "reviewing"

    configured_states = {
        str(resource.get("status", "")).upper() for resource in resources if resource.get("status")
    }
    effective_states = {
        str(resource.get("effective_status", "")).upper()
        for resource in resources
        if resource.get("effective_status")
    }
    active = (
        bool(configured_states)
        and configured_states == {"ACTIVE"}
        and effective_states <= {"ACTIVE"}
    )
    if not active:
        return "unknown"
    if _number(snapshot.get("impressions")) > 0:
        return "healthy"

    if published_at.tzinfo is None or published_at.utcoffset() is None:
        raise ValueError("published_at must be timezone-aware")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if now - published_at >= timedelta(hours=grace_hours):
        return "no_delivery"
    return "unknown"


def _number(value: object) -> Real:
    return value if isinstance(value, Real) and not isinstance(value, bool) else 0
