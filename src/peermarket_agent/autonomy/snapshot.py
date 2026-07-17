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
    basis = performance.get("autonomy_basis")
    if not isinstance(basis, Mapping):
        raise ValueError("persisted autonomy basis is missing")
    ids = basis.get("external_ids")
    campaign_id = basis.get("campaign_id")
    budget = basis.get("approved_budget_cents")
    window_start = _aware(basis.get("window_start"), "window_start")
    window_end = _aware(basis.get("window_end"), "window_end")
    captured_at = _aware(basis.get("captured_at"), "captured_at")
    if (
        not isinstance(campaign_id, str)
        or not campaign_id.isascii()
        or not campaign_id.isdecimal()
        or type(budget) is not int
        or budget <= 0
        or not isinstance(ids, Mapping)
        or ids.get("campaign_id") != campaign_id
        or not isinstance(ids.get("ad_set_id"), str)
        or not isinstance(ids.get("ad_id"), str)
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
        "frozen_basis": basis,
    }
    snapshot = {
        "snapshot_id": "autonomy:"
        + hashlib.sha256(
            json.dumps(
                stable,
                sort_keys=True,
                separators=(",", ":"),
                default=lambda value: (
                    value.isoformat() if isinstance(value, datetime) else str(value)
                ),
            ).encode()
        ).hexdigest(),
        "campaign_id": campaign_id,
        "captured_at": captured_at,
        "window_start": window_start,
        "window_end": window_end,
        "complete": basis.get("complete") is True,
        "delivery_state": basis.get("delivery_state", "unknown"),
        "attribution_complete": basis.get("attribution_complete") is True,
        "current_budget_cents": budget,
        "opening_budget_cents": budget,
        "allow_replacement": allow_replacement,
        "variants": list(variants),
        "replacement_source": replacement_source,
        "frozen_basis": dict(basis),
    }
    source_experiment = (
        replacement_source.get("experiment_id") if isinstance(replacement_source, Mapping) else None
    )
    if isinstance(source_experiment, str) and {item.get("variant_id") for item in variants} == {
        f"{source_experiment}:{number:02}" for number in (1, 2, 3)
    }:
        snapshot["experiment_id"] = source_experiment
    if reallocation is not None:
        snapshot["reallocation"] = dict(reallocation)
    return snapshot


def build_policy_decision(
    publication: Mapping[str, Any],
    variants: Sequence[Mapping[str, Any]],
    *,
    replacement_source: Mapping[str, Any] | None,
    history: Sequence[Mapping[str, Any]],
    limits: Mapping[str, Any] | object,
    now: datetime,
    allow_replacement: bool = True,
    reallocation: Mapping[str, Any] | None = None,
):
    """Public Task 7 seam from persisted collector evidence to a frozen decision."""
    from peermarket_agent.autonomy.policy import evaluate_campaign

    return evaluate_campaign(
        build_autonomy_snapshot(
            publication,
            variants,
            replacement_source=replacement_source,
            allow_replacement=allow_replacement,
            reallocation=reallocation,
        ),
        history,
        limits,
        now,
    )


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
        "complete": not bool((performance.get("meta") or {}).get("restated"))
        and (performance.get("meta") or {}).get("error") is None,
    }
