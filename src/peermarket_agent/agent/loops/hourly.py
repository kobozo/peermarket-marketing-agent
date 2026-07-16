"""Loop A — heartbeat, aggregate KPIs, and read-only Meta monitoring."""

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from peermarket_agent.meta_ads import MetaConfig, get_meta_ad_statuses
from peermarket_agent.meta_insights import fetch_meta_insights
from peermarket_agent.performance import classify_delivery, derive_performance
from peermarket_agent.publications import save_performance_snapshot

log = structlog.get_logger(__name__)


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
                        "SELECT draft_id, published_at, external_ids, performance "
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


async def _notify(notifier, message: str) -> None:
    if notifier is None:
        return
    try:
        await notifier.notify_founder(message)
    except Exception:
        log.exception("hourly_meta.alert_failed")


def _alert_payload(
    previous: dict, condition: str, statuses: dict, now: datetime
) -> tuple[dict, str | None]:
    problem = condition in {"no_delivery", "rejected_or_error"}
    old = previous.get("alert_state") or {}
    observed = {
        key: {
            field: value.get(field)
            for field in ("status", "effective_status", "issues")
            if value.get(field) is not None
        }
        for key, value in sorted(statuses.items())
        if isinstance(value, dict)
    }
    same_problem = (
        problem
        and old.get("condition") == condition
        and old.get("observed_state") == observed
        and old.get("active") is True
    )
    if problem:
        state = {
            "condition": condition,
            "observed_state": observed,
            "active": True,
            "changed_at": old.get("changed_at") if same_problem else now,
        }
        return state, None if same_problem else f"Meta delivery problem: {condition}"
    if old.get("active") is True:
        return {
            "condition": condition,
            "observed_state": observed,
            "active": False,
            "changed_at": now,
        }, f"Meta delivery recovered from {old.get('condition')}"
    return {
        "condition": condition,
        "observed_state": observed,
        "active": False,
        "changed_at": old.get("changed_at", now),
    }, None


async def collect_meta_performance(
    engine: AsyncEngine, settings, peermarket, notifier, now: datetime | None = None
) -> CollectionResult:
    """Collect each publication independently without mutating Meta resources."""
    now = now or datetime.now(UTC)
    start = (now - timedelta(days=2)).date()
    stop = now.date()
    publications = await _publications(engine)
    attribution = None
    attribution_error = False
    if settings.peermarket_attribution_enabled:
        try:
            attribution = await peermarket.fetch_attribution(start, stop)
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
            meta = derive_performance((previous.get("meta") or {}).get("latest"), current)
            meta.update({"statuses": statuses, "last_successful_retrieval": now, "error": None})
            condition = classify_delivery(statuses, current, publication["published_at"], now, 2)
            alert_state, alert = _alert_payload(previous, condition, statuses, now)
            payload = {
                "meta": meta,
                "delivery": {"condition": condition},
                "alert_state": alert_state,
            }
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
            await save_performance_snapshot(engine, draft_id, payload)
            updated.append(draft_id)
            if alert:
                await _notify(notifier, f"Draft #{draft_id}: {alert}")
        except Exception:
            log.warning("hourly_meta.publication_failed", draft_id=draft_id)
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

    if attribution_error and publications:
        already_alerted = all(
            ((row.get("performance") or {}).get("attribution") or {}).get("error")
            == "Aggregate attribution unavailable"
            for row in publications
        )
        if not already_alerted:
            await _notify(notifier, "Aggregate attribution unavailable; Meta collection continued")
    return CollectionResult(updated=updated, failed=failed)


async def run_hourly_pulse(
    engine: AsyncEngine, peermarket, *, settings=None, notifier=None
) -> None:
    await _record_heartbeat_and_site_kpis(engine, peermarket)
    if settings and settings.meta_insights_enabled:
        await collect_meta_performance(engine, settings, peermarket, notifier)
