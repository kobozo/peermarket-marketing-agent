"""Loop A — heartbeat, aggregate KPIs, and read-only Meta monitoring."""

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from peermarket_agent.agent.loops.autonomy import persist_autonomy_inputs, run_autonomy_cycle
from peermarket_agent.autonomy.snapshot import build_autonomy_basis
from peermarket_agent.meta_ads import MetaConfig, get_meta_ad_statuses
from peermarket_agent.meta_insights import fetch_meta_insights
from peermarket_agent.performance import classify_delivery, derive_performance
from peermarket_agent.publications import save_performance_snapshot

log = structlog.get_logger(__name__)
_ALERT_CLAIM_LEASE = timedelta(minutes=5)


@dataclass
class CollectionResult:
    updated: list[int]
    failed: list[int]


def _meta_config(settings) -> MetaConfig:
    return MetaConfig(
        app_id=settings.meta_app_id,
        app_secret=settings.meta_app_secret,
        system_user_token=settings.meta_system_user_token,
        ad_account_id=settings.meta_ad_account_id,
        page_id=settings.meta_page_id,
    )


async def _record_heartbeat_and_site_kpis(engine: AsyncEngine, peermarket) -> None:
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO kpis_hourly (ts, source, metric_name, value) "
                "VALUES (:ts, 'agent-internal', 'heartbeat', 1) "
                "ON CONFLICT (ts, source, metric_name) DO NOTHING"
            ),
            {"ts": now},
        )
        try:
            kpis = await peermarket.fetch_kpis()
            for name, value in kpis.items():
                await conn.execute(
                    text(
                        "INSERT INTO kpis_hourly (ts, source, metric_name, value) "
                        "VALUES (:ts, 'peermarket-prod', :n, :v) "
                        "ON CONFLICT (ts, source, metric_name) DO UPDATE "
                        "SET value = EXCLUDED.value"
                    ),
                    {"ts": now, "n": name, "v": float(value)},
                )
        except Exception:
            log.exception("hourly_pulse.peermarket_unreachable")
    log.info("hourly_pulse.complete", ts=now.isoformat())


async def _publications(engine: AsyncEngine) -> list[dict]:
    async with engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT draft_id, published_at, external_ids, approved_budget_cents, performance "
                        "FROM publications WHERE channel = 'meta' AND draft_id IS NOT NULL "
                        "AND external_ids->>'ad_id' IS NOT NULL "
                        "AND COALESCE(state, '') NOT IN ('terminal', 'archived', 'deleted') "
                        "ORDER BY draft_id"
                    )
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


async def _deliver(notifier, message: str) -> bool:
    if notifier is None:
        return False
    try:
        return bool(await notifier.notify_founder(message))
    except Exception:
        log.warning("hourly_meta.alert_failed")
        return False


def _observed_state(statuses: dict) -> dict:
    return {
        key: {
            field: value.get(field)
            for field in ("status", "effective_status", "issues")
            if value.get(field) is not None
        }
        for key, value in sorted(statuses.items())
        if isinstance(value, dict)
    }


def _claimed_at(claim: dict) -> datetime | None:
    try:
        value = datetime.fromisoformat(claim["claimed_at"])
    except (KeyError, TypeError, ValueError):
        return None
    return value if value.tzinfo is not None and value.utcoffset() is not None else None


async def _write_performance(conn, draft_id: int, performance: dict) -> None:
    await conn.execute(
        text(
            "UPDATE publications SET performance=CAST(:performance AS JSONB), "
            "updated_at=NOW() WHERE draft_id=:draft_id"
        ),
        {"draft_id": draft_id, "performance": json.dumps(performance)},
    )


async def _claim_alert(
    engine: AsyncEngine,
    draft_id: int,
    *,
    namespace: str,
    condition: str,
    observed_state: dict,
    active: bool,
    now: datetime,
) -> tuple[str, str] | None:
    """Claim one transition after re-evaluating durable state under row lock."""
    state_key = f"{namespace}_state"
    claim_key = f"{namespace}_claim"
    async with engine.begin() as conn:
        performance = (
            await conn.execute(
                text("SELECT performance FROM publications WHERE draft_id=:draft_id FOR UPDATE"),
                {"draft_id": draft_id},
            )
        ).scalar_one_or_none()
        if performance is None:
            return None
        performance = dict(performance)
        state = performance.get(state_key) or {}
        claim = performance.get(claim_key) or {}
        claimed_at = _claimed_at(claim)
        if claimed_at is not None and now - claimed_at < _ALERT_CLAIM_LEASE:
            return None
        if active:
            if (
                state.get("active") is True
                and state.get("condition") == condition
                and state.get("observed_state") == observed_state
            ):
                return None
            message = f"Meta delivery problem: {condition}"
        else:
            if state.get("active") is not True:
                return None
            message = f"Meta delivery recovered from {state.get('condition')}"
        token = str(uuid4())
        performance[claim_key] = {
            "claim_token": token,
            "claimed_at": now.isoformat(),
            "condition": condition,
            "observed_state": observed_state,
            "active": active,
        }
        await _write_performance(conn, draft_id, performance)
    return token, message


async def _finish_alert(
    engine: AsyncEngine,
    draft_id: int,
    *,
    namespace: str,
    token: str,
    delivered: bool,
    now: datetime,
) -> None:
    """Finalize confirmed delivery or release a failed claim for retry."""
    state_key = f"{namespace}_state"
    claim_key = f"{namespace}_claim"
    async with engine.begin() as conn:
        performance = (
            await conn.execute(
                text("SELECT performance FROM publications WHERE draft_id=:draft_id FOR UPDATE"),
                {"draft_id": draft_id},
            )
        ).scalar_one_or_none()
        if performance is None:
            return
        performance = dict(performance)
        claim = performance.get(claim_key) or {}
        if claim.get("claim_token") != token:
            return
        if delivered:
            performance[state_key] = {
                "condition": claim["condition"],
                "observed_state": claim["observed_state"],
                "active": claim["active"],
                "delivered_at": now.isoformat(),
            }
        performance.pop(claim_key, None)
        await _write_performance(conn, draft_id, performance)


async def _send_claimed_alert(
    engine: AsyncEngine,
    draft_id: int,
    notifier,
    *,
    namespace: str,
    condition: str,
    observed_state: dict,
    active: bool,
    now: datetime,
) -> None:
    claimed = await _claim_alert(
        engine,
        draft_id,
        namespace=namespace,
        condition=condition,
        observed_state=observed_state,
        active=active,
        now=now,
    )
    if claimed is None:
        return
    token, message = claimed
    delivered = await _deliver(notifier, f"Draft #{draft_id}: {message}")
    await _finish_alert(
        engine,
        draft_id,
        namespace=namespace,
        token=token,
        delivered=delivered,
        now=now,
    )


async def _claim_operational_alert(
    engine: AsyncEngine, *, active: bool, now: datetime
) -> tuple[str, str] | None:
    """Claim the singleton aggregate-attribution availability transition."""
    alert_key = "aggregate_attribution_availability"
    condition = "aggregate_attribution_unavailable" if active else "available"
    observed_state = {"available": not active}
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO operational_alert_state (alert_key) VALUES (:alert_key) "
                "ON CONFLICT (alert_key) DO NOTHING"
            ),
            {"alert_key": alert_key},
        )
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT state, claim FROM operational_alert_state "
                        "WHERE alert_key=:alert_key FOR UPDATE"
                    ),
                    {"alert_key": alert_key},
                )
            )
            .mappings()
            .one()
        )
        state = dict(row["state"] or {})
        claim = dict(row["claim"] or {})
        claimed_at = _claimed_at(claim)
        if claimed_at is not None and now - claimed_at < _ALERT_CLAIM_LEASE:
            return None
        if active:
            if state.get("active") is True and state.get("condition") == condition:
                return None
            message = "Aggregate attribution unavailable; Meta collection continued"
        else:
            if state.get("active") is not True:
                return None
            message = "Aggregate attribution recovered"
        token = str(uuid4())
        claim = {
            "claim_token": token,
            "claimed_at": now.isoformat(),
            "condition": condition,
            "observed_state": observed_state,
            "active": active,
        }
        await conn.execute(
            text(
                "UPDATE operational_alert_state SET claim=CAST(:claim AS JSONB), "
                "updated_at=NOW() WHERE alert_key=:alert_key"
            ),
            {"alert_key": alert_key, "claim": json.dumps(claim)},
        )
    return token, message


async def _finish_operational_alert(
    engine: AsyncEngine, *, token: str, delivered: bool, now: datetime
) -> None:
    alert_key = "aggregate_attribution_availability"
    async with engine.begin() as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT state, claim FROM operational_alert_state "
                        "WHERE alert_key=:alert_key FOR UPDATE"
                    ),
                    {"alert_key": alert_key},
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return
        claim = dict(row["claim"] or {})
        if claim.get("claim_token") != token:
            return
        state = dict(row["state"] or {})
        if delivered:
            state = {
                "condition": claim["condition"],
                "observed_state": claim["observed_state"],
                "active": claim["active"],
                "delivered_at": now.isoformat(),
            }
        await conn.execute(
            text(
                "UPDATE operational_alert_state SET state=CAST(:state AS JSONB), "
                "claim='{}'::JSONB, updated_at=NOW() WHERE alert_key=:alert_key"
            ),
            {"alert_key": alert_key, "state": json.dumps(state)},
        )


async def _send_attribution_availability_alert(
    engine: AsyncEngine, notifier, *, unavailable: bool, now: datetime
) -> None:
    claimed = await _claim_operational_alert(engine, active=unavailable, now=now)
    if claimed is None:
        return
    token, message = claimed
    delivered = await _deliver(notifier, message)
    await _finish_operational_alert(engine, token=token, delivered=delivered, now=now)


async def collect_meta_performance(
    engine: AsyncEngine, settings, peermarket, notifier, now: datetime | None = None
) -> CollectionResult:
    """Collect each publication independently without mutating Meta resources."""
    now = now or datetime.now(UTC)
    account_timezone_name = getattr(settings, "meta_account_timezone", "Europe/Brussels")
    lookback_days = getattr(settings, "meta_insights_lookback_days", 3)
    account_timezone = ZoneInfo(account_timezone_name)
    stop = now.astimezone(account_timezone).date() - timedelta(days=1)
    start = stop - timedelta(days=lookback_days - 1)
    utc_start = datetime.combine(start, datetime.min.time(), account_timezone).astimezone(UTC)
    utc_stop_exclusive = datetime.combine(
        stop + timedelta(days=1), datetime.min.time(), account_timezone
    ).astimezone(UTC)
    attribution_start = utc_start.date()
    attribution_stop = (utc_stop_exclusive - timedelta(microseconds=1)).date()
    publications = await _publications(engine)
    attribution = None
    attribution_error = False
    if settings.peermarket_attribution_enabled:
        try:
            attribution = await peermarket.fetch_attribution(attribution_start, attribution_stop)
        except Exception:
            attribution_error = True
            log.warning("hourly_meta.attribution_unavailable")

    updated: list[int] = []
    failed: list[int] = []
    config = _meta_config(settings)
    for publication in publications:
        draft_id = publication["draft_id"]
        ad_id = publication["external_ids"]["ad_id"]
        previous = publication.get("performance") or {}
        try:
            statuses = await get_meta_ad_statuses(config, publication["external_ids"])
            snapshot = await fetch_meta_insights(config, ad_id, start, stop)
            current = dict(vars(snapshot))
            current["window_start"] = start
            current["window_stop"] = stop
            current["window_definition"] = (
                f"rolling-{(stop - start).days + 1}-inclusive-calendar-days"
            )
            current["account_timezone"] = account_timezone_name
            current["account_window"] = {
                "start": start.isoformat(),
                "stop": stop.isoformat(),
            }
            current["utc_alignment"] = {
                "start": utc_start.isoformat(),
                "stop_exclusive": utc_stop_exclusive.isoformat(),
                "overlap_start_day": attribution_start.isoformat(),
                "overlap_stop_day": attribution_stop.isoformat(),
            }
            meta = derive_performance((previous.get("meta") or {}).get("latest"), current)
            meta.update({"statuses": statuses, "last_successful_retrieval": now, "error": None})
            condition = classify_delivery(
                statuses,
                current,
                publication["published_at"],
                now,
                getattr(settings, "meta_no_delivery_grace_hours", 2),
            )
            payload = {"meta": meta, "delivery": {"condition": condition}}
            if settings.peermarket_attribution_enabled:
                if attribution_error:
                    payload["attribution"] = {
                        "available": False,
                        "error": "Aggregate attribution unavailable",
                    }
                else:
                    payload["attribution"] = {
                        "available": True,
                        "error": None,
                        "events": [
                            asdict(row)
                            for row in attribution
                            if row.utm_content == f"draft-{draft_id}"
                        ],
                    }
            merged_for_basis = dict(previous)
            merged_for_basis.update(payload)
            if not settings.peermarket_attribution_enabled:
                merged_for_basis["attribution"] = {"available": False}
            payload["autonomy_basis"] = build_autonomy_basis(publication, merged_for_basis)
            await save_performance_snapshot(engine, draft_id, payload)
            updated.append(draft_id)
            await _send_claimed_alert(
                engine,
                draft_id,
                notifier,
                namespace="alert",
                condition=condition,
                observed_state=_observed_state(statuses),
                active=condition in {"no_delivery", "rejected_or_error"},
                now=now,
            )
        except Exception as error:
            log.warning(
                "hourly_meta.publication_failed",
                draft_id=draft_id,
                error_type=type(error).__name__,
            )
            failure_payload = {
                "meta": {"error": "Meta performance collection failed", "failed_at": now}
            }
            if attribution_error:
                failure_payload["attribution"] = {
                    "available": False,
                    "error": "Aggregate attribution unavailable",
                }
            try:
                await save_performance_snapshot(engine, draft_id, failure_payload)
            except Exception:
                log.warning("hourly_meta.failure_diagnostic_not_saved", draft_id=draft_id)
            failed.append(draft_id)

    if settings.peermarket_attribution_enabled:
        await _send_attribution_availability_alert(
            engine, notifier, unavailable=attribution_error, now=now
        )
    return CollectionResult(updated=updated, failed=failed)


async def run_hourly_pulse(
    engine: AsyncEngine, peermarket, *, settings=None, notifier=None, claude=None
) -> None:
    await _record_heartbeat_and_site_kpis(engine, peermarket)
    if settings and settings.meta_insights_enabled:
        await collect_meta_performance(engine, settings, peermarket, notifier)
        await persist_autonomy_inputs(engine)
        await run_autonomy_cycle(engine, claude, notifier, settings)
