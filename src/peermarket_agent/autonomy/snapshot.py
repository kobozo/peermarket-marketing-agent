"""Canonical autonomous evidence derived from the real performance document."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any


def _aware(value: object, name: str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def build_autonomy_snapshot(
    publication: Mapping[str, Any],
    variants: Sequence[Mapping[str, Any]],
    *,
    replacement_source: Mapping[str, Any] | None,
    allow_replacement: bool = True,
    reallocation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build policy/executor evidence from persisted hourly namespaces only."""
    performance = publication.get("performance")
    if not isinstance(performance, Mapping):
        raise ValueError("publication performance is missing")
    meta = performance.get("meta")
    delivery = performance.get("delivery")
    attribution = performance.get("attribution")
    if not all(isinstance(item, Mapping) for item in (meta, delivery, attribution)):
        raise ValueError("performance namespaces are incomplete")
    latest = meta.get("latest")
    if not isinstance(latest, Mapping) or meta.get("error") is not None:
        raise ValueError("latest Meta evidence is unavailable")
    alignment = latest.get("utc_alignment")
    if not isinstance(alignment, Mapping):
        raise ValueError("Meta evidence window is unavailable")
    window_start = _aware(alignment.get("start"), "window_start")
    window_end = _aware(alignment.get("stop_exclusive"), "window_end")
    captured_at = _aware(meta.get("last_successful_retrieval"), "captured_at")
    ids = publication.get("external_ids")
    campaign_id = ids.get("campaign_id") if isinstance(ids, Mapping) else None
    budget = publication.get("approved_budget_cents")
    if (
        not isinstance(campaign_id, str)
        or not campaign_id.isascii()
        or not campaign_id.isdecimal()
        or type(budget) is not int
        or budget <= 0
        or not variants
    ):
        raise ValueError("publication identity or budget is incomplete")
    stable = {
        "campaign_id": campaign_id,
        "captured_at": captured_at.isoformat(),
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "variants": list(variants),
        "source": replacement_source,
    }
    snapshot = {
        "snapshot_id": "autonomy:"
        + hashlib.sha256(
            json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "campaign_id": campaign_id,
        "captured_at": captured_at,
        "window_start": window_start,
        "window_end": window_end,
        "complete": not bool(meta.get("restated")),
        "delivery_state": delivery.get("condition", "unknown"),
        "attribution_complete": attribution.get("available") is True,
        "current_budget_cents": budget,
        "opening_budget_cents": budget,
        "allow_replacement": allow_replacement,
        "variants": list(variants),
        "replacement_source": replacement_source,
    }
    if reallocation is not None:
        snapshot["reallocation"] = dict(reallocation)
    return snapshot


def build_autonomy_basis(
    publication: Mapping[str, Any], performance: Mapping[str, Any]
) -> dict[str, Any]:
    """Freeze only facts the per-publication hourly collector can truthfully know."""
    meta = performance.get("meta") or {}
    latest = meta.get("latest") or {}
    alignment = latest.get("utc_alignment") or {}
    ids = publication.get("external_ids") or {}
    return {
        "campaign_id": ids.get("campaign_id"),
        "external_ids": dict(ids),
        "approved_budget_cents": publication.get("approved_budget_cents"),
        "captured_at": meta.get("last_successful_retrieval"),
        "window_start": alignment.get("start"),
        "window_end": alignment.get("stop_exclusive"),
        "delivery_state": (performance.get("delivery") or {}).get("condition"),
        "attribution_complete": (performance.get("attribution") or {}).get("available") is True,
    }
