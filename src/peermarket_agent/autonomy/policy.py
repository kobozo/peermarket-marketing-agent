"""Pure, deterministic autonomous campaign decision policy."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import cmp_to_key
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from peermarket_agent.autonomy.contracts import DecisionKind, FrozenDecision

_DIMENSIONS = (
    "channel",
    "objective",
    "language",
    "audience",
    "creative_dimension",
    "window_definition",
)
_VARIANT_FIELDS = {
    "variant_id",
    "publication_id",
    *_DIMENSIONS,
    "impressions",
    "landing_page_views",
    "registrations",
}
_MUTATIONS = {"pause", "replace", "reallocate", "scale"}
_DELIVERY_FAILURES = {"no_delivery", "rejected_or_error"}
_FALLBACK_END = datetime(1970, 1, 1, 0, 0, 0, 1, tzinfo=UTC)


class _InvalidEvidence(ValueError):
    pass


def evaluate_campaign(
    snapshot: Mapping[str, Any],
    history: Sequence[Mapping[str, Any]],
    limits: Mapping[str, Any] | object,
    now: datetime,
) -> FrozenDecision:
    """Derive one immutable decision without performing I/O."""
    try:
        _aware(now, "now")
        normalized = _normalize_snapshot(snapshot)
        normalized_history = _normalize_history(history, now)
        policy = _normalize_limits(limits)
    except _InvalidEvidence:
        return _observe_from_untrusted(snapshot, now, "invalid_snapshot")
    except (TypeError, ValueError, OverflowError):
        return _observe_from_untrusted(snapshot, now, "invalid_snapshot")

    normalized["policy_limits"] = {
        "snapshot_age_hours": policy["snapshot_age"],
        "min_impressions": policy["impressions"],
        "min_landing_page_views": policy["views"],
        "min_registrations": policy["registrations"],
        "cooldown_hours": policy["cooldown"],
        "max_test_days": policy["max_test_days"],
        "max_replacements_24h": policy["max_replacements"],
        "max_increase_percent": policy["increase_percent"],
        "max_daily_budget_cents": policy["ceiling_eur"] * 100,
        "no_delivery_grace_hours": policy["no_delivery_grace"],
        "complete_window_required": True,
        "account_timezone": policy["account_timezone"],
    }

    if normalized_history is None:
        return _decision(DecisionKind.OBSERVE, normalized, (), "invalid_history")

    ordered_history = normalized_history
    if not _completed_window(normalized, now):
        return _decision(DecisionKind.OBSERVE, normalized, ordered_history, "incomplete_window")
    if not _fresh(normalized, policy, now):
        return _decision(DecisionKind.OBSERVE, normalized, ordered_history, "stale_snapshot")
    if not normalized["attribution_complete"]:
        return _decision(DecisionKind.OBSERVE, normalized, ordered_history, "missing_attribution")
    if normalized["delivery_state"] in _DELIVERY_FAILURES:
        if normalized["delivery_state"] == "no_delivery" and (
            now - normalized["configured_active_since"]
            < timedelta(hours=policy["no_delivery_grace"])
        ):
            return _decision(
                DecisionKind.OBSERVE,
                normalized,
                ordered_history,
                "no_delivery_grace_period",
            )
        return _decision(
            DecisionKind.OBSERVE,
            normalized,
            ordered_history,
            f"diagnose_{normalized['delivery_state']}",
        )
    if normalized["delivery_state"] != "healthy":
        return _decision(DecisionKind.OBSERVE, normalized, ordered_history, "delivery_unavailable")
    if _in_cooldown(ordered_history, policy, now):
        return _decision(DecisionKind.OBSERVE, normalized, ordered_history, "mutation_cooldown")

    variants = normalized["variants"]
    if not _comparable(variants):
        return _decision(DecisionKind.OBSERVE, normalized, ordered_history, "not_comparable")
    if not _evidence_floors(variants, policy):
        duration = normalized["window_end"] - normalized["window_start"]
        reason = (
            "maximum_test_duration_without_qualified_comparison"
            if duration >= timedelta(days=policy["max_test_days"])
            else "insufficient_evidence"
        )
        return _decision(DecisionKind.OBSERVE, normalized, ordered_history, reason)

    ordered = sorted(variants, key=cmp_to_key(_compare_variants))
    if _compare_rates(ordered[0], ordered[-1]) == 0:
        return _decision(DecisionKind.OBSERVE, normalized, ordered_history, "neutral_tie")
    loser, winner = ordered[0], ordered[-1]
    winner_value = _rate(winner)
    loser_value = _rate(loser)
    outcome = {
        "winner_variant_id": winner["variant_id"],
        "loser_variant_id": loser["variant_id"],
        "winner_value": str(winner_value),
        "loser_value": str(loser_value),
        "metric": "registration_per_landing_page_view",
    }

    reallocation = normalized.get("reallocation")
    if reallocation is not None:
        if reallocation["old_budget_cents"] != reallocation["new_budget_cents"]:
            return _decision(
                DecisionKind.OBSERVE, normalized, ordered_history, "invalid_reallocation"
            )
        return _decision(
            DecisionKind.REALLOCATE,
            normalized,
            ordered_history,
            "proven_winner_reallocate",
            outcome,
            reallocation["old_budget_cents"],
            reallocation["new_budget_cents"],
            reallocation["allocations"],
        )

    if normalized["allow_replacement"]:
        if normalized.get("replacement_source") is None:
            return _decision(
                DecisionKind.OBSERVE,
                normalized,
                ordered_history,
                "missing_replacement_source",
                outcome,
            )
        replacements = sum(
            event["kind"] == "replace" and event["at"] > now - timedelta(hours=24)
            for event in ordered_history
        )
        if replacements >= policy["max_replacements"]:
            return _decision(
                DecisionKind.OBSERVE, normalized, ordered_history, "replacement_limit", outcome
            )
        return _decision(
            DecisionKind.REPLACE,
            normalized,
            ordered_history,
            "proven_loser_replace",
            outcome,
        )

    return _scale(normalized, ordered_history, policy, now, outcome)


def _normalize_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(snapshot, Mapping):
        raise _InvalidEvidence
    campaign_id = snapshot.get("campaign_id")
    if not isinstance(campaign_id, str) or not campaign_id.isascii() or not campaign_id.isdecimal():
        raise _InvalidEvidence
    snapshot_id = _stable_id(snapshot.get("snapshot_id"), "snapshot_id")
    window_start = _aware(snapshot.get("window_start"), "window_start")
    window_end = _aware(snapshot.get("window_end"), "window_end")
    captured_at = _aware(snapshot.get("captured_at"), "captured_at")
    if (
        type(snapshot.get("complete")) is not bool
        or type(snapshot.get("attribution_complete")) is not bool
    ):
        raise _InvalidEvidence
    delivery_state = snapshot.get("delivery_state")
    if delivery_state not in {"healthy", "unknown", "reviewing", "terminal", *_DELIVERY_FAILURES}:
        raise _InvalidEvidence
    variants_value = snapshot.get("variants")
    if not isinstance(variants_value, (list, tuple)) or not variants_value:
        raise _InvalidEvidence
    variants = tuple(
        sorted(
            (_normalize_variant(item) for item in variants_value),
            key=lambda item: item["variant_id"],
        )
    )
    if len({item["variant_id"] for item in variants}) != len(variants):
        raise _InvalidEvidence("variant_id must be unique")
    current_budget = _positive_int(snapshot.get("current_budget_cents"), "current_budget_cents")
    opening = snapshot.get("opening_budget_cents", 1_000)
    opening_budget = _positive_int(opening, "opening_budget_cents")
    allow_replacement = snapshot.get("allow_replacement", True)
    if type(allow_replacement) is not bool:
        raise _InvalidEvidence
    normalized: dict[str, Any] = {
        "snapshot_id": snapshot_id,
        "campaign_id": campaign_id,
        "captured_at": captured_at,
        "window_start": window_start,
        "window_end": window_end,
        "complete": snapshot["complete"],
        "delivery_state": delivery_state,
        "attribution_complete": snapshot["attribution_complete"],
        "current_budget_cents": current_budget,
        "opening_budget_cents": opening_budget,
        "allow_replacement": allow_replacement,
        "variants": variants,
    }
    frozen_basis = snapshot.get("frozen_basis")
    if frozen_basis is not None:
        if not isinstance(frozen_basis, Mapping):
            raise _InvalidEvidence
        normalized["frozen_basis"] = dict(frozen_basis)
    if delivery_state == "no_delivery":
        configured_active_since = _aware(
            snapshot.get("configured_active_since"), "configured_active_since"
        )
        if configured_active_since > captured_at:
            raise _InvalidEvidence("configured_active_since cannot follow capture")
        normalized["configured_active_since"] = configured_active_since
    if "reallocation" in snapshot:
        item = snapshot["reallocation"]
        if not isinstance(item, Mapping):
            raise _InvalidEvidence
        normalized["reallocation"] = {
            "old_budget_cents": _positive_int(item.get("old_budget_cents"), "old_budget_cents"),
            "new_budget_cents": _positive_int(item.get("new_budget_cents"), "new_budget_cents"),
            "allocations": _normalize_allocations(item.get("allocations")),
        }
    source = snapshot.get("replacement_source")
    if source is not None:
        normalized["replacement_source"] = _normalize_replacement_source(source, campaign_id)
    return normalized


def _normalize_variant(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) - _VARIANT_FIELDS:
        raise _InvalidEvidence
    variant_id = value.get("variant_id")
    if not isinstance(variant_id, str) or not variant_id.strip():
        raise _InvalidEvidence
    publication_id = _positive_int(value.get("publication_id"), "publication_id")
    normalized = {"variant_id": variant_id, "publication_id": publication_id}
    for dimension in _DIMENSIONS:
        item = value.get(dimension)
        if not isinstance(item, str) or not item.strip():
            raise _InvalidEvidence
        normalized[dimension] = item
    for counter in ("impressions", "landing_page_views", "registrations"):
        normalized[counter] = _counter(value.get(counter), counter)
    return normalized


def _normalize_history(
    history: Sequence[Mapping[str, Any]], now: datetime
) -> tuple[dict[str, Any], ...] | None:
    if isinstance(history, (str, bytes)) or not isinstance(history, Sequence):
        return None
    result = []
    try:
        for item in history:
            if not isinstance(item, Mapping):
                return None
            kind = item.get("kind")
            if not isinstance(kind, str) or not kind:
                return None
            event = {
                "event_id": _stable_id(item.get("event_id"), "event_id"),
                "kind": kind,
                "at": _aware(item.get("at"), "history.at"),
            }
            if event["at"] > now:
                return None
            if "old_budget_cents" in item or "new_budget_cents" in item:
                event["old_budget_cents"] = _positive_int(
                    item.get("old_budget_cents"), "old_budget_cents"
                )
                event["new_budget_cents"] = _positive_int(
                    item.get("new_budget_cents"), "new_budget_cents"
                )
            result.append(event)
    except (_InvalidEvidence, TypeError, ValueError):
        return None
    if len({item["event_id"] for item in result}) != len(result):
        return None
    return tuple(sorted(result, key=lambda item: (item["at"], item["event_id"], item["kind"])))


def _normalize_limits(limits: Mapping[str, Any] | object) -> dict[str, Any]:
    def read(name: str) -> Any:
        return limits.get(name) if isinstance(limits, Mapping) else getattr(limits, name)

    names = {
        "snapshot_age": "performance_snapshot_max_age_hours",
        "impressions": "learning_min_impressions",
        "views": "learning_min_landing_page_views",
        "registrations": "learning_min_registrations",
        "cooldown": "meta_autonomy_cooldown_hours",
        "max_test_days": "meta_autonomy_max_test_days",
        "max_replacements": "meta_autonomy_max_replacements_24h",
        "increase_percent": "meta_autonomy_max_increase_percent",
        "ceiling_eur": "meta_autonomy_max_daily_budget_eur",
        "no_delivery_grace": "meta_no_delivery_grace_hours",
    }
    normalized = {key: _counter(read(name), name) for key, name in names.items()}
    timezone = read("meta_account_timezone")
    if not isinstance(timezone, str) or not timezone.strip():
        raise _InvalidEvidence("meta_account_timezone must be a non-empty IANA zone")
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise _InvalidEvidence("meta_account_timezone must be a valid IANA zone") from exc
    normalized["account_timezone"] = timezone
    return normalized


def _scale(snapshot, history, policy, now, outcome):
    current = snapshot["current_budget_cents"]
    window = tuple(event for event in history if event["at"] > now - timedelta(hours=24))
    if any(event["kind"] == "scale" for event in window):
        return _decision(DecisionKind.OBSERVE, snapshot, history, "increase_limit", outcome)
    budget_events = tuple(event for event in window if "old_budget_cents" in event)
    if current != snapshot["opening_budget_cents"] and not budget_events:
        return _decision(DecisionKind.OBSERVE, snapshot, history, "missing_budget_history", outcome)
    opening = (
        budget_events[0]["old_budget_cents"] if budget_events else snapshot["opening_budget_cents"]
    )
    increased = sum(
        max(0, event["new_budget_cents"] - event["old_budget_cents"]) for event in budget_events
    )
    permitted = opening * policy["increase_percent"] // 100
    headroom = permitted - increased
    ceiling = policy["ceiling_eur"] * 100
    proposed = min(current + max(0, headroom), opening + permitted, ceiling)
    if proposed <= current:
        reason = "absolute_budget_ceiling" if current >= ceiling else "increase_headroom_exhausted"
        return _decision(DecisionKind.OBSERVE, snapshot, history, reason, outcome)
    allocations = _scale_allocations(snapshot, current, proposed)
    if allocations is None and (snapshot.get("frozen_basis") or {}).get("campaign_publications"):
        return _decision(DecisionKind.OBSERVE, snapshot, history, "invalid_campaign_allocations")
    return _decision(
        DecisionKind.SCALE,
        snapshot,
        history,
        "proven_winner_scale",
        outcome,
        current,
        proposed,
        allocations,
    )


def _scale_allocations(snapshot, old_total, new_total):
    publications = (snapshot.get("frozen_basis") or {}).get("campaign_publications")
    if publications is None:
        return None
    if not isinstance(publications, (list, tuple)) or not publications:
        return None
    prepared = []
    try:
        for raw in publications:
            ids = raw["external_ids"]
            old = _positive_int(raw["approved_budget_cents"], "approved_budget_cents")
            if ids["campaign_id"] != snapshot["campaign_id"]:
                return None
            prepared.append(
                {
                    "publication_id": _positive_int(raw["publication_id"], "publication_id"),
                    "variant_id": str(_positive_int(raw["draft_id"], "draft_id")),
                    "campaign_id": snapshot["campaign_id"],
                    "ad_set_id": _stable_id(ids["ad_set_id"], "ad_set_id"),
                    "ad_id": _stable_id(ids["ad_id"], "ad_id"),
                    "old_budget_cents": old,
                }
            )
    except (KeyError, TypeError, _InvalidEvidence):
        return None
    prepared.sort(key=lambda item: (item["publication_id"], item["ad_set_id"], item["ad_id"]))
    if sum(item["old_budget_cents"] for item in prepared) != old_total:
        return None
    floors = [item["old_budget_cents"] * new_total // old_total for item in prepared]
    remainder = new_total - sum(floors)
    order = sorted(
        range(len(prepared)),
        key=lambda index: (
            -(prepared[index]["old_budget_cents"] * new_total % old_total),
            prepared[index]["publication_id"],
            prepared[index]["ad_set_id"],
        ),
    )
    for index in order[:remainder]:
        floors[index] += 1
    return {
        str(item["publication_id"]): {**item, "new_budget_cents": floors[index]}
        for index, item in enumerate(prepared)
    }


def _completed_window(snapshot, now):
    return snapshot["complete"] and snapshot["window_start"] < snapshot["window_end"] <= now


def _fresh(snapshot, policy, now):
    return timedelta(0) <= now - snapshot["captured_at"] <= timedelta(hours=policy["snapshot_age"])


def _comparable(variants):
    return (
        len(variants) >= 2
        and len({tuple(item[key] for key in _DIMENSIONS) for item in variants}) == 1
    )


def _evidence_floors(variants, policy):
    return all(
        item["impressions"] >= policy["impressions"]
        and item["landing_page_views"] >= policy["views"]
        and item["registrations"] >= policy["registrations"]
        for item in variants
    )


def _rate(variant):
    return Decimal(variant["registrations"]) / Decimal(variant["landing_page_views"])


def _compare_rates(left, right):
    difference = (
        left["registrations"] * right["landing_page_views"]
        - right["registrations"] * left["landing_page_views"]
    )
    return (difference > 0) - (difference < 0)


def _compare_variants(left, right):
    rate_order = _compare_rates(left, right)
    if rate_order:
        return rate_order
    return (right["variant_id"] > left["variant_id"]) - (right["variant_id"] < left["variant_id"])


def _in_cooldown(history, policy, now):
    boundary = now - timedelta(hours=policy["cooldown"])
    return any(event["kind"] in _MUTATIONS and event["at"] > boundary for event in history)


def _normalize_allocations(value):
    if not isinstance(value, Mapping) or set(value) != {"winner", "loser"}:
        raise _InvalidEvidence
    result = {}
    for label, item in value.items():
        if not isinstance(item, Mapping) or set(item) != {
            "campaign_id",
            "variant_id",
            "ad_set_id",
            "ad_id",
            "old_budget_cents",
            "new_budget_cents",
        }:
            raise _InvalidEvidence
        ad_set_id = _stable_id(item.get("ad_set_id"), "ad_set_id")
        ad_id = _stable_id(item.get("ad_id"), "ad_id")
        if not all(value.isascii() and value.isdecimal() for value in (ad_set_id, ad_id)):
            raise _InvalidEvidence
        result[label] = {
            "campaign_id": _stable_id(item.get("campaign_id"), "campaign_id"),
            "variant_id": _stable_id(item.get("variant_id"), "variant_id"),
            "ad_set_id": ad_set_id,
            "ad_id": ad_id,
            "old_budget_cents": _positive_int(item.get("old_budget_cents"), "old_budget_cents"),
            "new_budget_cents": _positive_int(item.get("new_budget_cents"), "new_budget_cents"),
        }
    return result


def _normalize_replacement_source(value, campaign_id):
    if not isinstance(value, Mapping):
        raise _InvalidEvidence
    required = {
        "draft_id",
        "campaign_id",
        "experiment_id",
        "changed_dimension",
        "locales",
        "audience_profile_key",
        "image_prompt",
        "asset_path",
        "daily_budget_eur",
        "landing_page_url",
        "publication_id",
        "objective",
        "current_meta_ids",
    }
    if set(value) != required or value.get("campaign_id") != campaign_id:
        raise _InvalidEvidence
    if type(value.get("draft_id")) is not int or value["draft_id"] <= 0:
        raise _InvalidEvidence
    if type(value.get("publication_id")) is not int or value["publication_id"] <= 0:
        raise _InvalidEvidence
    if value.get("objective") != "OUTCOME_TRAFFIC":
        raise _InvalidEvidence
    if value.get("changed_dimension") not in {"hook", "copy", "visual", "audience"}:
        raise _InvalidEvidence
    if type(value.get("daily_budget_eur")) is not int or not 5 <= value["daily_budget_eur"] <= 20:
        raise _InvalidEvidence
    for key in (
        "experiment_id",
        "audience_profile_key",
        "image_prompt",
        "asset_path",
        "landing_page_url",
    ):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise _InvalidEvidence
    locales = value.get("locales")
    locale_fields = {"locale", "hook", "body", "headline", "description", "cta_label"}
    if not isinstance(locales, Mapping) or set(locales) != {"NL", "FR", "EN"}:
        raise _InvalidEvidence
    if any(
        not isinstance(item, Mapping)
        or set(item) != locale_fields
        or item.get("locale") != locale
        or any(
            not isinstance(item.get(field), str) or not item[field].strip()
            for field in locale_fields
        )
        for locale, item in locales.items()
    ):
        raise _InvalidEvidence
    ids = value.get("current_meta_ids")
    if (
        not isinstance(ids, Mapping)
        or set(ids) != {"campaign_id", "ad_set_id", "ad_ids", "creative_ids"}
        or ids.get("campaign_id") != campaign_id
    ):
        raise _InvalidEvidence
    if not isinstance(ids.get("ad_set_id"), str) or not ids["ad_set_id"].isdecimal():
        raise _InvalidEvidence
    for key in ("ad_ids", "creative_ids"):
        mapping = ids.get(key)
        if (
            not isinstance(mapping, Mapping)
            or set(mapping) != {"NL", "FR", "EN"}
            or any(not isinstance(item, str) or not item.isdecimal() for item in mapping.values())
        ):
            raise _InvalidEvidence
    return {key: value[key] for key in required}


def _decision(
    kind,
    snapshot,
    history,
    reason,
    outcome=None,
    old_budget=None,
    new_budget=None,
    allocations=None,
):
    evidence = {
        "snapshot_id": snapshot["snapshot_id"],
        "delivery_state": snapshot["delivery_state"],
        "attribution_complete": snapshot["attribution_complete"],
        "variants": [_json_variant(item) for item in snapshot["variants"]],
    }
    if "configured_active_since" in snapshot:
        evidence["configured_active_since"] = snapshot["configured_active_since"].isoformat()
    if outcome:
        evidence.update(outcome)
    if allocations is not None:
        evidence["allocations"] = allocations
    if "policy_limits" in snapshot:
        evidence["policy_limits"] = snapshot["policy_limits"]
    if kind is DecisionKind.REPLACE:
        evidence["source"] = snapshot["replacement_source"]
    if "frozen_basis" in snapshot:
        evidence["frozen_basis"] = snapshot["frozen_basis"]
    canonical = {
        "campaign_id": snapshot["campaign_id"],
        "window_start": snapshot["window_start"].isoformat(),
        "window_end": snapshot["window_end"].isoformat(),
        "kind": kind.value,
        "reason": reason,
        "evidence": evidence,
        "history": [_json_event(item) for item in history],
        "old_budget_cents": old_budget,
        "new_budget_cents": new_budget,
    }
    digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return FrozenDecision(
        kind=kind,
        campaign_id=snapshot["campaign_id"],
        window_start=snapshot["window_start"],
        window_end=snapshot["window_end"],
        evidence=evidence,
        reason=reason,
        idempotency_key=f"autonomy:{digest}",
        old_budget_cents=old_budget,
        new_budget_cents=new_budget,
        allocations=allocations,
    )


def _observe_from_untrusted(snapshot, now, reason):
    campaign_id = snapshot.get("campaign_id") if isinstance(snapshot, Mapping) else None
    if not isinstance(campaign_id, str) or not campaign_id.isascii() or not campaign_id.isdecimal():
        campaign_id = "0"
    start = snapshot.get("window_start") if isinstance(snapshot, Mapping) else None
    end = snapshot.get("window_end") if isinstance(snapshot, Mapping) else None
    if not _is_aware(start) or not _is_aware(end) or start >= end:
        end = now if _is_aware(now) else _FALLBACK_END
        start = end - timedelta(microseconds=1)
    snapshot_id = snapshot.get("snapshot_id") if isinstance(snapshot, Mapping) else None
    if not isinstance(snapshot_id, str) or not snapshot_id.strip():
        snapshot_id = "invalid"
    safe = {
        "snapshot_id": snapshot_id,
        "campaign_id": campaign_id,
        "window_start": start,
        "window_end": end,
        "delivery_state": "unknown",
        "attribution_complete": False,
        "variants": (
            {
                "variant_id": "invalid",
                "publication_id": 0,
                **{key: "invalid" for key in _DIMENSIONS},
                "impressions": 0,
                "landing_page_views": 0,
                "registrations": 0,
            },
        ),
    }
    return _decision(DecisionKind.OBSERVE, safe, (), reason)


def _json_variant(item):
    return {key: item[key] for key in sorted(item)}


def _json_event(item):
    return {
        key: (value.isoformat() if isinstance(value, datetime) else value)
        for key, value in sorted(item.items())
    }


def _counter(value, name):
    if type(value) is not int or value < 0:
        raise _InvalidEvidence(f"{name} must be a non-negative integer")
    return value


def _positive_int(value, name):
    value = _counter(value, name)
    if value == 0:
        raise _InvalidEvidence(f"{name} must be positive")
    return value


def _stable_id(value, name):
    if not isinstance(value, str) or not value.strip():
        raise _InvalidEvidence(f"{name} must be a non-empty string")
    return value


def _aware(value, name):
    if not _is_aware(value):
        raise _InvalidEvidence(f"{name} must be timezone-aware")
    return value


def _is_aware(value):
    return (
        isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None
    )
