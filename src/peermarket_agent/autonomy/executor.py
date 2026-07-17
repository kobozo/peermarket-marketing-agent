"""Fenced execution and compensation for autonomous Meta lifecycle actions."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import text

from peermarket_agent.autonomy.contracts import ActionStatus, DecisionKind
from peermarket_agent.autonomy.store import (
    begin_execution,
    block_campaign_for_reconciliation,
    campaign_history,
    finish_action,
    release_action,
)

_HEARTBEAT_SECONDS = 30.0
_RATE_LIMIT_CODES = {4, 17, 32, 613}
_ACTIVE_EFFECTIVE = {"ACTIVE", "IN_PROCESS", "PENDING_REVIEW"}
_PAUSED_EFFECTIVE = {
    "PAUSED",
    "CAMPAIGN_PAUSED",
    "ADSET_PAUSED",
    "PENDING_REVIEW",
    "IN_PROCESS",
    "PREAPPROVED",
    "PENDING_BILLING_INFO",
}


class ExecutionStatus(StrEnum):
    REFUSED = "refused"
    CANCELLED = "cancelled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RETRYABLE = "retryable"
    RECONCILIATION_REQUIRED = "reconciliation_required"


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    status: ExecutionStatus
    reason: str
    before_state: Any = None
    after_state: Any = None
    rollback_result: Any = None
    retry_at: datetime | None = None


class _AmbiguousExternalWrite(RuntimeError):
    """An external mutation failed without proving whether Meta applied it."""


def _setting(settings: object, name: str, default: Any = None) -> Any:
    return getattr(settings, name, default)


async def _call(target: object, name: str, *args: Any, **kwargs: Any) -> Any:
    function = getattr(target, name)
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        return await function(*args, **kwargs)
    if not any(p.kind is p.VAR_KEYWORD for p in signature.parameters.values()):
        kwargs = {key: value for key, value in kwargs.items() if key in signature.parameters}
    value = function(*args, **kwargs)
    return await value if inspect.isawaitable(value) else value


def _plain(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    return value


def _rate_limited(exc: BaseException) -> bool:
    return (
        getattr(exc, "http_status", None) == 429
        or getattr(exc, "api_error_code", None) in _RATE_LIMIT_CODES
        or getattr(exc, "error_code", None) in _RATE_LIMIT_CODES
    )


async def _renew_action(engine: Any, claim: Any, seconds: int = 300) -> None:
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "UPDATE autonomous_actions SET lease_expires_at=NOW()+make_interval(secs=>:seconds), updated_at=NOW() WHERE id=:id AND status='executing' AND lease_owner=:owner AND lease_token=:token AND lease_expires_at>NOW()"
            ),
            {
                "seconds": seconds,
                "id": claim.id,
                "owner": claim.lease_owner,
                "token": claim.lease_token,
            },
        )
        if result.rowcount != 1:
            raise RuntimeError("execution lease ownership was lost")


async def _external(
    engine: Any, claim: Any, target: object, name: str, *args: Any, **kwargs: Any
) -> Any:
    await _renew_action(engine, claim)
    return await _call(target, name, *args, **kwargs)


async def _write_external(
    engine: Any, claim: Any, target: object, name: str, *args: Any, **kwargs: Any
) -> Any:
    try:
        return await _external(engine, claim, target, name, *args, **kwargs)
    except asyncio.CancelledError:
        raise
    except BaseException as exc:
        raise _AmbiguousExternalWrite(name) from exc


async def _heartbeat(engine: Any, claim: Any, lost: asyncio.Event) -> None:
    try:
        while True:
            await asyncio.sleep(_HEARTBEAT_SECONDS)
            await _renew_action(engine, claim)
    except asyncio.CancelledError:
        raise
    except Exception:
        lost.set()


async def _publication(engine: Any, campaign_id: str) -> dict[str, Any] | None:
    async with engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT p.draft_id, p.state, p.external_ids, p.approved_budget_cents, p.performance FROM publications p WHERE p.channel='meta' AND p.external_ids->>'campaign_id'=:campaign ORDER BY p.updated_at DESC LIMIT 1"
                    ),
                    {"campaign": campaign_id},
                )
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None


def _ids(publication: Mapping[str, Any]) -> dict[str, str]:
    raw = publication.get("external_ids") or {}
    return {key: str(raw[key]) for key in ("campaign_id", "ad_set_id", "ad_id") if raw.get(key)}


def _source_ok(
    source: Mapping[str, Any], campaign: str, ids: Mapping[str, str], budget: int
) -> bool:
    return (
        str(source.get("campaign_id")) == campaign
        and all(
            not ids.get(k) or str(source.get(k)) == ids[k]
            for k in ("campaign_id", "ad_set_id", "ad_id")
        )
        and source.get("budget_cents", source.get("daily_budget")) == budget
        and source.get("status") == "ACTIVE"
        and source.get("effective_status") in _ACTIVE_EFFECTIVE
    )


def _history_events(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events = []
    for row in rows:
        if row.get("status") == "succeeded":
            events.append({"kind": row.get("kind"), "at": row.get("updated_at")})
        for event in row.get("budget_events", ()):
            events.append({"kind": "budget", "at": event["created_at"], **event})
    return events


def _policy_reason(
    claim: Any,
    settings: Any,
    publication: Mapping[str, Any],
    source: Mapping[str, Any],
    history: list[dict[str, Any]],
    now: datetime,
) -> str | None:
    decision = claim.decision
    snapshot_id = decision.evidence.get("snapshot_id")
    performance = publication.get("performance") or {}
    current_snapshot = performance.get("snapshot_id") or performance.get("current", {}).get(
        "snapshot_id"
    )
    if current_snapshot is not None and current_snapshot != snapshot_id:
        return "stale_snapshot"
    budget = publication.get("approved_budget_cents")
    if not isinstance(budget, int) or not _source_ok(
        source, claim.campaign_id, _ids(publication), budget
    ):
        return "live_state_changed"
    boundary = now - timedelta(hours=_setting(settings, "meta_autonomy_cooldown_hours", 24))
    if any(
        e.get("at")
        and e["at"] > boundary
        and e.get("kind") in {"pause", "replace", "reallocate", "scale"}
        for e in history
    ):
        return "cooldown"
    if decision.kind is DecisionKind.REPLACE:
        replacements = sum(
            e.get("kind") == "replace" and e.get("at") and e["at"] > now - timedelta(hours=24)
            for e in history
        )
        if replacements >= _setting(settings, "meta_autonomy_max_replacements_24h", 1):
            return "replacement_limit"
    if decision.kind in {DecisionKind.SCALE, DecisionKind.REALLOCATE}:
        if decision.old_budget_cents != budget:
            return "budget_changed"
        new = decision.new_budget_cents
        if (
            not isinstance(new, int)
            or new > _setting(settings, "meta_autonomy_max_daily_budget_eur", 20) * 100
        ):
            return "budget_cap"
        if decision.kind is DecisionKind.SCALE:
            opening = next(
                (
                    e["old_budget_cents"]
                    for e in history
                    if e.get("kind") == "budget"
                    and e.get("at")
                    and e["at"] > now - timedelta(hours=24)
                ),
                budget,
            )
            used = sum(
                max(0, e["new_budget_cents"] - e["old_budget_cents"])
                for e in history
                if e.get("kind") == "budget" and e.get("at") and e["at"] > now - timedelta(hours=24)
            )
            cap = opening * _setting(settings, "meta_autonomy_max_increase_percent", 20) // 100
            if new - budget > cap - used or new > opening + cap:
                return "increase_cap"
    return None


def _bundle_ids(bundle: Any) -> dict[str, Any]:
    value = _plain(bundle)
    result = {
        "campaign_id": value["campaign_id"],
        "ad_set_id": value["ad_set_id"],
        "ad_ids": value["ad_ids"],
    }
    if set(result["ad_ids"]) != {"NL", "FR", "EN"}:
        raise RuntimeError("replacement_bundle_must_contain_exact_locales")
    creative_ids = value.get("creative_ids")
    if creative_ids is not None and set(creative_ids) != {"NL", "FR", "EN"}:
        raise RuntimeError("replacement_bundle_must_contain_exact_creatives")
    return result


def _bundle_verified(state: Mapping[str, Any], active: bool) -> bool:
    keys = {"campaign", "ad_set", "ad:NL", "ad:FR", "ad:EN"}
    if not keys.issubset(state):
        return False
    wanted = "ACTIVE" if active else "PAUSED"
    effective = _ACTIVE_EFFECTIVE if active else _PAUSED_EFFECTIVE
    return all(
        state[key].get("status") == wanted and state[key].get("effective_status") in effective
        for key in keys
    )


async def _replace(
    engine: Any, settings: Any, meta: Any, builder: Any, claim: Any, source: Mapping[str, Any]
) -> tuple[Any, Any]:
    draft = (
        await _call(builder, "build", engine=engine, settings=settings, claim=claim)
        if hasattr(builder, "build")
        else await builder(engine, settings, claim)
    )
    bundle = await _write_external(engine, claim, meta, "create_paused", draft=draft, claim=claim)
    ids = _bundle_ids(bundle)
    try:
        paused = await _external(engine, claim, meta, "read_replacement", **ids)
        if not _bundle_verified(paused, False):
            raise RuntimeError("replacement_not_paused")
        await _write_external(engine, claim, meta, "activate_replacement", **ids)
        active = await _external(engine, claim, meta, "read_replacement", **ids)
        if not _bundle_verified(active, True):
            raise RuntimeError("replacement_not_active")
        try:
            await _write_external(engine, claim, meta, "pause_source", source=source)
            source_after = await _external(engine, claim, meta, "read_source", claim.campaign_id)
            if (
                source_after.get("status") != "PAUSED"
                or source_after.get("effective_status") not in _PAUSED_EFFECTIVE
            ):
                raise RuntimeError("source_not_paused")
        except BaseException:
            await _write_external(engine, claim, meta, "pause_replacement", **ids)
            verified = await _external(engine, claim, meta, "read_replacement", **ids)
            if not _bundle_verified(verified, False):
                raise RuntimeError("replacement_rollback_unproven") from None
            raise
        return active, {"source": source_after}
    except BaseException:
        with suppress(Exception):
            await _write_external(engine, claim, meta, "pause_replacement", **ids)
        raise


async def execute_claim(
    engine: Any, settings: Any, meta: Any, replacement_builder: Any, claim: Any, now: datetime
) -> ExecutionResult:
    """Execute one claim through the complete fail-closed lifecycle."""
    if not _setting(settings, "meta_autonomy_enabled", False):
        return ExecutionResult(ExecutionStatus.REFUSED, "disabled")
    if _setting(settings, "meta_autonomy_shadow", True):
        return ExecutionResult(ExecutionStatus.REFUSED, "shadow_mode")
    if claim.campaign_id not in tuple(_setting(settings, "meta_autonomy_campaign_ids", ())):
        return ExecutionResult(ExecutionStatus.REFUSED, "not_allowlisted")
    if engine is None or not await begin_execution(engine, claim):
        return ExecutionResult(ExecutionStatus.REFUSED, "lease_lost")
    publication = await _publication(engine, claim.campaign_id)
    if (
        publication is None
        or len(_ids(publication)) != 3
        or publication.get("state") not in {"active", "published"}
    ):
        await finish_action(
            engine, claim, status=ActionStatus.CANCELLED, failure_category="publication_changed"
        )
        return ExecutionResult(ExecutionStatus.CANCELLED, "publication_changed")
    lost = asyncio.Event()
    heartbeat = asyncio.create_task(_heartbeat(engine, claim, lost))
    before = None
    try:
        before = await _external(engine, claim, meta, "read_source", claim.campaign_id)
        history = _history_events(await campaign_history(engine, claim.campaign_id))
        reason = _policy_reason(claim, settings, publication, before, history, now)
        if reason:
            await finish_action(
                engine,
                claim,
                status=ActionStatus.CANCELLED,
                before_state=before,
                failure_category=reason,
            )
            return ExecutionResult(ExecutionStatus.CANCELLED, reason, before_state=before)
        if claim.kind is DecisionKind.PAUSE:
            await _write_external(engine, claim, meta, "pause_source", source=before)
            after = await _external(engine, claim, meta, "read_source", claim.campaign_id)
            if after.get("status") != "PAUSED":
                raise RuntimeError("pause_verification_failed")
            budget_event = None
        elif claim.kind in {DecisionKind.SCALE, DecisionKind.REALLOCATE}:
            await _write_external(
                engine,
                claim,
                meta,
                "set_budget",
                ad_set_id=before["ad_set_id"],
                cents=claim.decision.new_budget_cents,
            )
            after = await _external(engine, claim, meta, "read_source", claim.campaign_id)
            if (
                after.get("budget_cents", after.get("daily_budget"))
                != claim.decision.new_budget_cents
            ):
                raise RuntimeError("budget_verification_failed")
            budget_event = (claim.decision.old_budget_cents, claim.decision.new_budget_cents)
        elif claim.kind is DecisionKind.REPLACE:
            after, _ = await _replace(engine, settings, meta, replacement_builder, claim, before)
            budget_event = None
        else:
            after, budget_event = before, None
        if lost.is_set():
            raise RuntimeError("execution lease ownership was lost")
        if not await finish_action(
            engine,
            claim,
            status=ActionStatus.SUCCEEDED,
            before_state=before,
            after_state=after,
            budget=budget_event,
        ):
            raise RuntimeError("execution finalization fence was lost")
        return ExecutionResult(ExecutionStatus.SUCCEEDED, "executed", before, after)
    except BaseException as exc:
        if isinstance(exc, asyncio.CancelledError):
            raise
        if not isinstance(exc, _AmbiguousExternalWrite) and _rate_limited(exc):
            retry_at = now + timedelta(minutes=5)
            await release_action(
                engine, claim, failure_category="meta_rate_limit", next_evaluation_at=retry_at
            )
            return ExecutionResult(
                ExecutionStatus.RETRYABLE, "meta_rate_limit", before, retry_at=retry_at
            )
        reconciled = await block_campaign_for_reconciliation(
            engine,
            claim,
            before_state=before,
            failure_category="external_state_unproven",
            failure_message=type(exc).__name__,
        )
        if reconciled:
            return ExecutionResult(
                ExecutionStatus.RECONCILIATION_REQUIRED, "external_state_unproven", before
            )
        return ExecutionResult(ExecutionStatus.FAILED, "lease_lost", before)
    finally:
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat
