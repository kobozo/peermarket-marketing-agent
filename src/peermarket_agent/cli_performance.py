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

from peermarket_agent.agent.loops.autonomy import prepare_hook_experiment
from peermarket_agent.config import get_settings
from peermarket_agent.mcp_servers.peermarket_readonly import PeermarketReadonly
from peermarket_agent.meta_ads import MetaConfig, get_meta_ad_statuses
from peermarket_agent.meta_insights import fetch_meta_insights

_CLOCK_SKEW_TOLERANCE = timedelta(minutes=5)
_NEUTRAL_EXPERIMENT_REASONS = {
    "insufficient_evidence",
    "neutral_tie",
    "maximum_test_duration_without_qualified_comparison",
    "stale_snapshot",
    "missing_attribution",
    "not_comparable",
    "technical_delivery_failure",
}
_QUALIFIED_EXPERIMENT_REASONS = {
    "proven_loser_replace",
    "proven_winner_reallocate",
    "proven_winner_scale",
}


def classify_experiment_reason(reason: str) -> str:
    if reason in _NEUTRAL_EXPERIMENT_REASONS:
        return "neutral"
    if reason in _QUALIFIED_EXPERIMENT_REASONS:
        return "qualified"
    raise ValueError("unknown experiment policy reason")


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


def _safe_autonomy_evidence(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    allowed = {
        "snapshot_id",
        "delivery_state",
        "attribution_complete",
        "winner_variant_id",
        "loser_variant_id",
        "winner_value",
        "loser_value",
        "metric",
    }
    return {key: value[key] for key in sorted(allowed & value.keys())}


def _safe_experiment_evidence(value: object, reason: str | None) -> dict[str, Any]:
    if not isinstance(value, dict):
        value = {}
    result = {"reason": reason}
    for key in ("experiment_id", "variant_ids", "delivery_state", "attribution_complete"):
        if key in value:
            result[key] = value[key]
    result["samples"] = [
        {
            key: item[key]
            for key in ("variant_id", "impressions", "landing_page_views", "registrations")
            if key in item
        }
        for item in value.get("variants", [])
        if isinstance(item, dict)
    ]
    if "variant_ids" not in result:
        result["variant_ids"] = [
            item["variant_id"] for item in result["samples"] if "variant_id" in item
        ]
    result["thresholds"] = {
        key: value["policy_limits"][key]
        for key in (
            "min_impressions",
            "min_landing_page_views",
            "min_registrations",
            "cooldown_hours",
            "max_test_days",
            "max_increase_percent",
            "max_daily_budget_cents",
            "max_replacements_24h",
            "snapshot_age_hours",
            "no_delivery_grace_hours",
            "complete_window_required",
            "account_timezone",
        )
        if isinstance(value.get("policy_limits"), dict) and key in value["policy_limits"]
    }
    result["window"] = {
        key: value["evidence_window"][key]
        for key in ("start", "end", "window_start", "window_end", "captured_at")
        if isinstance(value.get("evidence_window"), dict) and key in value["evidence_window"]
    }
    return result


async def inspect_autonomy(draft_id: int) -> dict[str, Any]:
    """Return a fixed, sanitized autonomous-lifecycle projection for one draft."""
    settings = get_settings()
    engine = create_async_engine(settings.agent_db_url, future=True, pool_pre_ping=True)
    try:
        async with readonly_connection(engine) as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            "SELECT p.draft_id,p.state AS publication_state,p.approved_budget_cents,"
                            "p.external_ids->>'campaign_id' AS campaign_id,d.id AS decision_id,"
                            "p.external_ids->>'ad_set_id' AS publication_ad_set_id,dr.metadata AS draft_metadata,"
                            "d.kind,d.reason,d.window_start,d.window_end,d.evidence,"
                            "d.old_budget_cents,d.new_budget_cents,a.id AS action_id,a.status AS action_status,"
                            "a.failure_category,a.next_evaluation_at,a.audit,"
                            "(SELECT count(*) FROM autonomous_actions aa WHERE aa.campaign_id="
                            "p.external_ids->>'campaign_id' AND aa.status NOT IN "
                            "('succeeded','failed','cancelled')) AS active_action_count,"
                            "(SELECT COALESCE(jsonb_agg(DISTINCT aa.status),'[]'::jsonb) "
                            "FROM autonomous_actions aa WHERE aa.campaign_id="
                            "p.external_ids->>'campaign_id' AND aa.status NOT IN "
                            "('succeeded','failed','cancelled')) AS active_action_statuses,"
                            "(SELECT jsonb_build_object('id',o.id,'status',o.status,"
                            "'idempotency_key',o.idempotency_key,'decision_id',d.id,"
                            "'experiment_id',o.payload->'experiment_id','variant_ids',"
                            "o.payload->'variant_ids','evidence_window',o.payload->'evidence_window') "
                            "FROM slack_outbox o WHERE o.autonomy_campaign_id="
                            "p.external_ids->>'campaign_id' AND o.message_kind='autonomy_audit' "
                            "AND o.idempotency_key LIKE 'autonomy:'||d.decision_key||':%' "
                            "ORDER BY o.id DESC LIMIT 1) AS slack_audit "
                            "FROM publications p JOIN drafts dr ON dr.id=p.draft_id "
                            "LEFT JOIN LATERAL (SELECT * FROM autonomous_decisions "
                            "WHERE campaign_id=p.external_ids->>'campaign_id' ORDER BY id DESC LIMIT 1) d "
                            "ON TRUE LEFT JOIN LATERAL (SELECT * FROM autonomous_actions "
                            "WHERE decision_id=d.id ORDER BY id DESC LIMIT 1) a ON TRUE "
                            "WHERE p.draft_id=:draft_id AND p.channel='meta'"
                        ),
                        {"draft_id": draft_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
            experiment_id = settings.meta_autonomy_experiment_id
            experiment_rows = []
            if experiment_id:
                experiment_rows = [
                    dict(item)
                    for item in (
                        await connection.execute(
                            text(
                                "SELECT experiment_id,variant_id,language,campaign_id,ad_set_id,"
                                "landing_page_url,changed_dimension,fixed_identity "
                                "FROM autonomous_hook_experiment_variants "
                                "WHERE experiment_id=:experiment_id ORDER BY variant_id,language"
                            ),
                            {"experiment_id": experiment_id},
                        )
                    ).mappings()
                ]
        report: dict[str, Any] = {
            "draft_id": draft_id,
            "feature_flags": {
                "enabled": settings.meta_autonomy_enabled,
                "shadow": settings.meta_autonomy_shadow,
                "allowlisted": False,
            },
            "publication_exists": row is not None,
            "hook_experiment": _hook_experiment_status(
                experiment_rows,
                settings=settings,
                draft_id=draft_id,
                expected=(
                    {
                        "campaign_id": row["campaign_id"],
                        "ad_set_id": row["publication_ad_set_id"],
                        "landing_page_url": (row["draft_metadata"] or {}).get("landing_page_url"),
                        "fixed_identity": (row["draft_metadata"] or {}).get("fixed_identity"),
                    }
                    if row is not None
                    else None
                ),
            ),
        }
        if row is None:
            return report
        campaign_id = row["campaign_id"]
        report["feature_flags"]["allowlisted"] = campaign_id in tuple(
            settings.meta_autonomy_campaign_ids
        )
        report["campaign_id"] = campaign_id
        report["publication_state"] = row["publication_state"]
        report["budget"] = {
            "approved_cents": row["approved_budget_cents"],
            "old_cents": row["old_budget_cents"],
            "new_cents": row["new_budget_cents"],
        }
        report["decision"] = (
            {
                "id": row["decision_id"],
                "kind": row["kind"],
                "reason": row["reason"],
                "window_start": row["window_start"],
                "window_end": row["window_end"],
                "evidence": _safe_autonomy_evidence(row["evidence"]),
            }
            if row["decision_id"] is not None
            else None
        )
        report["experiment_evidence"] = _safe_experiment_evidence(row["evidence"], row["reason"])
        report["active_action_count"] = int(row["active_action_count"] or 0)
        report["active_action_statuses"] = list(row["active_action_statuses"] or [])
        report["slack_audit"] = dict(row["slack_audit"]) if row["slack_audit"] else None
        audit = row["audit"] if isinstance(row["audit"], dict) else {}
        report["action"] = (
            {
                "id": row["action_id"],
                "status": row["action_status"],
                "failure_category": row["failure_category"],
                "next_evaluation_at": row["next_evaluation_at"],
                "rollback_recorded": "rollback_result" in audit,
            }
            if row["action_id"] is not None
            else None
        )
        report["reconciliation_blocked"] = row["action_status"] == "reconciliation_required"
        return report
    finally:
        await engine.dispose()


def _hook_experiment_status(
    rows: list[dict[str, Any]], *, settings: Any, draft_id: int, expected: dict | None
) -> dict:
    """Build a copy-free, identifier-only readiness projection."""
    experiment_id = str(getattr(settings, "meta_autonomy_experiment_id", "") or "")
    grouped: dict[str, set[str]] = {}
    identities = set()
    campaigns = set()
    for row in rows:
        grouped.setdefault(str(row["variant_id"]), set()).add(str(row["language"]))
        identities.add(
            json.dumps(
                [
                    row["campaign_id"],
                    row["ad_set_id"],
                    row["landing_page_url"],
                    row["changed_dimension"],
                    row["fixed_identity"],
                ],
                sort_keys=True,
                default=str,
            )
        )
        campaigns.add(str(row["campaign_id"]))
    exact_bundle = len(grouped) == 3 and all(v == {"NL", "FR", "EN"} for v in grouped.values())
    expected_variant_ids = {f"{experiment_id}:{number:02}" for number in (1, 2, 3)}
    variant_ids_match = set(grouped) == expected_variant_ids
    fixed_identity_match = exact_bundle and len(identities) == 1
    allowlisted = campaigns == {"120249125021520342"} and campaigns <= set(
        getattr(settings, "meta_autonomy_campaign_ids", ())
    )
    persisted_identity_match = bool(expected) and all(
        row["experiment_id"] == experiment_id
        and row["campaign_id"] == expected.get("campaign_id")
        and row["ad_set_id"] == expected.get("ad_set_id")
        and row["landing_page_url"] == expected.get("landing_page_url")
        and row["changed_dimension"] == "hook"
        and row["fixed_identity"] == expected.get("fixed_identity")
        for row in rows
    )
    blocked = None
    if draft_id != 156:
        blocked = "draft_not_156"
    elif not experiment_id:
        blocked = "experiment_not_configured"
    elif not getattr(settings, "meta_autonomy_shadow", True):
        blocked = "shadow_mode_required"
    elif not variant_ids_match:
        blocked = "variant_ids_mismatch"
    elif not exact_bundle:
        blocked = "experiment_incomplete"
    elif not persisted_identity_match:
        blocked = "persisted_identity_mismatch"
    elif not fixed_identity_match:
        blocked = "fixed_identity_mismatch"
    elif not allowlisted:
        blocked = "campaign_not_allowlisted"
    return {
        "experiment_id": experiment_id or None,
        "variant_count": len(grouped),
        "variants": [
            {"variant_id": key, "languages": sorted(value)}
            for key, value in sorted(grouped.items())
        ],
        "fixed_identity_match": fixed_identity_match,
        "ready": blocked is None,
        "blocked_reason": blocked,
    }


async def prepare_hook_experiment_command(draft_id: int, seed: str) -> dict[str, Any]:
    """Load the fixed draft inputs and persist its local shadow experiment."""
    settings = get_settings()
    engine = create_async_engine(settings.agent_db_url, future=True, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            "SELECT d.id,d.metadata,p.external_ids,b.voice_rules_md FROM drafts d "
                            "JOIN publications p ON p.draft_id=d.id AND p.channel='meta' "
                            "JOIN brand_voice b ON b.id=1 WHERE d.id=:draft_id"
                        ),
                        {"draft_id": draft_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise ValueError("Draft 156 publication or brand voice is unavailable")
        metadata = dict(row["metadata"] or {})
        external_ids = dict(row["external_ids"] or {})
        draft = {
            "id": row["id"],
            "campaign_id": external_ids.get("campaign_id"),
            "ad_set_id": external_ids.get("ad_set_id"),
            "landing_page_url": metadata.get("landing_page_url"),
            "fixed_identity": metadata.get("fixed_identity"),
            "language_bundles": metadata.get("language_bundles"),
        }
        experiment = await prepare_hook_experiment(
            engine, settings, draft, str(row["voice_rules_md"]), seed
        )
        return {
            "draft_id": draft_id,
            "experiment_id": experiment.experiment_id,
            "variant_ids": [variant.variant_id for variant in experiment.variants],
            "languages": ["NL", "FR", "EN"],
            "shadow": True,
        }
    finally:
        await engine.dispose()


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


@cli.command("autonomy")
@click.option("--draft-id", required=True, type=click.IntRange(min=1))
def autonomy(draft_id: int) -> None:
    """Inspect persisted autonomy state without permitting a database write."""
    click.echo(json.dumps(asyncio.run(inspect_autonomy(draft_id)), sort_keys=True, default=str))


@cli.command("prepare-hook-experiment")
@click.option("--draft-id", required=True, type=click.IntRange(min=1))
@click.option("--seed", default="draft-156-shadow-v1", show_default=True)
def prepare_hook_experiment_cli(draft_id: int, seed: str) -> None:
    """Persist the fixed shadow experiment locally; never mutate Meta."""
    try:
        report = asyncio.run(prepare_hook_experiment_command(draft_id, seed))
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    cli()
