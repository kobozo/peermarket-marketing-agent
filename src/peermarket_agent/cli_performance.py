"""Read-only production checks for Meta performance collection."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import click
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from peermarket_agent.config import get_settings
from peermarket_agent.mcp_servers.peermarket_readonly import PeermarketReadonly
from peermarket_agent.meta_ads import MetaConfig, get_meta_ad_statuses
from peermarket_agent.meta_insights import fetch_meta_insights

_CLOCK_SKEW_TOLERANCE = timedelta(minutes=5)


@asynccontextmanager
async def readonly_connection(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """Open a PostgreSQL transaction that rejects every write statement."""
    async with engine.begin() as connection:
        await connection.execute(text("SET TRANSACTION READ ONLY"))
        yield connection


async def read_publication(dsn: str, draft_id: int) -> dict[str, Any] | None:
    """Read the minimum publication fields needed by the verifier."""
    engine = create_async_engine(dsn, future=True, pool_pre_ping=True)
    try:
        async with readonly_connection(engine) as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            "SELECT id, draft_id, state, "
                            "CASE WHEN external_id IS NOT NULL "
                            "THEN jsonb_build_object('ad_id', external_id) "
                            "ELSE '{}'::JSONB END || COALESCE(external_ids, '{}'::JSONB) "
                            "AS external_ids, performance FROM publications "
                            "WHERE draft_id = :draft_id"
                        ),
                        {"draft_id": draft_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
        return dict(row) if row is not None else None
    finally:
        await engine.dispose()


async def read_meta_statuses(config: MetaConfig, ids: dict[str, str]) -> dict:
    """Read current Meta hierarchy statuses without updating resources."""
    return await get_meta_ad_statuses(config, ids)


async def read_meta_insights(config: MetaConfig, ad_id: str, start: date, stop: date):
    """Read the configured Meta Insights window."""
    return await fetch_meta_insights(config, ad_id, start, stop)


async def read_attribution(dsn: str, start: date, stop: date):
    """Read only the fixed production attribution aggregate."""
    client = PeermarketReadonly(dsn)
    try:
        return await client.fetch_attribution(start, stop)
    finally:
        await client._engine.dispose()


def _meta_config(settings) -> MetaConfig:
    return MetaConfig(
        app_id=settings.meta_app_id,
        app_secret=settings.meta_app_secret,
        system_user_token=settings.meta_system_user_token,
        ad_account_id=settings.meta_ad_account_id,
        page_id=settings.meta_page_id,
    )


def _safe_statuses(statuses: dict) -> dict:
    return {
        name: {
            key: value
            for key, value in status.items()
            if key in {"status", "effective_status"} and isinstance(value, str)
        }
        for name, status in statuses.items()
        if isinstance(name, str) and isinstance(status, dict)
    }


def _snapshot_is_fresh(
    value: object,
    now: datetime,
    *,
    max_age_hours: int,
    clock_skew_tolerance: timedelta = _CLOCK_SKEW_TOLERANCE,
) -> bool:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return False
    if not isinstance(value, datetime) or value.tzinfo is None:
        return False
    age = now - value.astimezone(UTC)
    return -clock_skew_tolerance <= age <= timedelta(hours=max_age_hours)


def _verification_failures(report: dict[str, Any]) -> list[str]:
    if report.get("publication_read_available") is False:
        return ["publication_read_failed"]
    if report.get("publication_exists") is False:
        return ["publication_missing"]
    failures = []
    flags = report["feature_flags"]
    if flags["meta_insights_enabled"]:
        if report["meta_status"] != "available":
            failures.append("meta_unavailable")
        elif not report["snapshot_fresh"]:
            failures.append("snapshot_stale")
    if flags["peermarket_attribution_enabled"] and report["attribution_status"] != "available":
        failures.append("attribution_unavailable")
    return failures


async def verify_draft(draft_id: int) -> dict[str, Any]:
    """Check collection dependencies and return only sanitized operational facts."""
    settings = get_settings()
    report: dict[str, Any] = {
        "draft_id": draft_id,
        "feature_flags": {
            "meta_insights_enabled": settings.meta_insights_enabled,
            "peermarket_attribution_enabled": settings.peermarket_attribution_enabled,
        },
        "publication_exists": False,
        "meta_available": False,
        "attribution_available": False,
        "meta_status": "unavailable" if settings.meta_insights_enabled else "disabled",
        "attribution_status": (
            "unavailable" if settings.peermarket_attribution_enabled else "disabled"
        ),
        "snapshot_fresh": False,
    }
    try:
        publication = await read_publication(settings.agent_db_url, draft_id)
        report["publication_read_available"] = True
    except Exception:
        report["publication_read_available"] = False
        return report
    if publication is None:
        return report

    ids = {
        key: value
        for key, value in (publication.get("external_ids") or {}).items()
        if key in {"campaign_id", "ad_set_id", "creative_id", "ad_id"} and isinstance(value, str)
    }
    report["publication_exists"] = True
    report["publication"] = {
        "id": publication.get("id"),
        "draft_id": publication.get("draft_id"),
        "state": publication.get("state"),
        "external_ids": ids,
    }
    now = datetime.now(UTC)
    last_retrieval = ((publication.get("performance") or {}).get("meta") or {}).get(
        "last_successful_retrieval"
    )
    report["snapshot_fresh"] = _snapshot_is_fresh(
        last_retrieval,
        now,
        max_age_hours=settings.performance_snapshot_max_age_hours,
    )
    account_timezone = ZoneInfo(settings.meta_account_timezone)
    stop = now.astimezone(account_timezone).date() - timedelta(days=1)
    start = stop - timedelta(days=settings.meta_insights_lookback_days - 1)
    utc_start = datetime.combine(start, datetime.min.time(), account_timezone).astimezone(UTC)
    utc_stop_exclusive = datetime.combine(
        stop + timedelta(days=1), datetime.min.time(), account_timezone
    ).astimezone(UTC)

    if settings.meta_insights_enabled:
        try:
            statuses = await read_meta_statuses(_meta_config(settings), ids)
            snapshot = await read_meta_insights(_meta_config(settings), ids["ad_id"], start, stop)
            report["meta_available"] = True
            report["meta_status"] = "available"
            report["meta_statuses"] = _safe_statuses(statuses)
            report["meta_counts"] = {
                "impressions": int(snapshot.impressions),
                "landing_page_views": int(snapshot.landing_page_views),
            }
        except Exception:
            pass

    if settings.peermarket_attribution_enabled:
        try:
            aggregates = await read_attribution(
                settings.peermarket_prod_db_readonly_url,
                utc_start.date(),
                (utc_stop_exclusive - timedelta(microseconds=1)).date(),
            )
            matching = [row for row in aggregates if row.utm_content == f"draft-{draft_id}"]
            report["attribution_available"] = True
            report["attribution_status"] = "available"
            counts: dict[str, int] = {}
            for row in matching:
                count = getattr(row, "event_count", getattr(row, "count", 0))
                counts[row.event_type] = counts.get(row.event_type, 0) + int(count)
            report["attribution_counts"] = counts
        except Exception:
            pass
    return report


@click.group()
def cli() -> None:
    """Read-only performance operations."""


@cli.command("verify")
@click.option("--draft-id", required=True, type=click.IntRange(min=1))
def verify(draft_id: int) -> None:
    """Verify live read paths and snapshot freshness for one publication."""
    report = asyncio.run(verify_draft(draft_id))
    click.echo(json.dumps(report, sort_keys=True))
    failures = _verification_failures(report)
    if failures:
        raise click.ClickException("verification failed: " + ", ".join(failures))


if __name__ == "__main__":
    cli()
