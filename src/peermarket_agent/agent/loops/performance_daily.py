"""Daily, observation-only summaries of attributed campaign evidence."""

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from peermarket_agent.learnings import (
    DEFAULT_THRESHOLDS,
    EvidenceThresholds,
    EvidenceVariant,
    eligible_learning,
)

log = structlog.get_logger(__name__)
_SUMMARY_CLAIM_LEASE = timedelta(minutes=5)


@dataclass(frozen=True)
class EvidenceObservation:
    metrics: dict[str, int | str | Decimal | None]


def safe_ratio(numerator: int | None, denominator: int | None) -> Decimal | None:
    if numerator is None or denominator in (None, 0):
        return None
    return Decimal(numerator) / Decimal(denominator)


def evaluate_publication(performance: dict) -> EvidenceObservation:
    """Derive ratios without converting absent attribution into a zero."""
    latest = (performance.get("meta") or {}).get("latest") or performance.get("meta") or {}
    attribution = performance.get("attribution") or {}
    events = attribution.get("events", []) if attribution.get("available") is True else []
    event_counts = {
        event_type: _event_count(events, event_type)
        for event_type in (
            "landing_view",
            "registration_completed",
            "first_listing_created",
            "first_listing_published",
            "identity_verification_completed",
        )
    }
    spend = _optional_count(latest.get("spend_cents"))
    impressions = _optional_count(latest.get("impressions"))
    clicks = _optional_count(latest.get("clicks"))
    link_clicks = _optional_count(latest.get("inline_link_clicks"))
    meta_landing_page_views = _optional_count(latest.get("landing_page_views"))
    first_party_landing_views = event_counts["landing_view"]
    registrations = event_counts["registration_completed"]
    first_listing_created = event_counts["first_listing_created"]
    first_listing_published = event_counts["first_listing_published"]
    identity_verifications = event_counts["identity_verification_completed"]
    return EvidenceObservation(
        metrics={
            "approved_budget_cents": _optional_count(performance.get("approved_budget_cents")),
            "spend_cents": spend,
            "delivery_state": (performance.get("delivery") or {}).get("condition"),
            "impressions": impressions,
            "clicks": clicks,
            "link_clicks": link_clicks,
            "meta_landing_page_views": meta_landing_page_views,
            "first_party_landing_views": first_party_landing_views,
            "registrations": registrations,
            "first_listing_created": first_listing_created,
            "first_listing_published": first_listing_published,
            "identity_verifications": identity_verifications,
            "cost_per_link_click_cents": safe_ratio(spend, link_clicks),
            "click_to_meta_landing": safe_ratio(meta_landing_page_views, link_clicks),
            "first_party_landing_to_registration": safe_ratio(
                registrations, first_party_landing_views
            ),
            "cost_per_registration_cents": safe_ratio(spend, registrations),
            "registration_to_first_listing": safe_ratio(first_listing_created, registrations),
            "cost_per_first_published_listing_cents": safe_ratio(spend, first_listing_published),
            "identity_verification_conversion": safe_ratio(identity_verifications, registrations),
        }
    )


def _count(value: object) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _optional_count(value: object) -> int | None:
    return (
        _count(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    )


def _event_count(events: object, event_type: str) -> int | None:
    matching = [
        event
        for event in events
        if isinstance(event, dict) and event.get("event_type") == event_type
    ]
    if not matching:
        return None
    return sum(_count(event.get("event_count")) for event in matching)


def _date(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _valid_window(latest: dict) -> tuple[date, date, str] | None:
    start = _date(latest.get("window_start"))
    stop = _date(latest.get("window_stop"))
    definition = latest.get("window_definition")
    if (
        start is None
        or stop is None
        or start > stop
        or not isinstance(definition, str)
        or not definition.strip()
    ):
        return None
    return start, stop, definition


def _json_metrics(
    metrics: dict[str, int | str | Decimal | None],
) -> dict[str, int | str | None]:
    return {
        name: str(value) if isinstance(value, Decimal) else value for name, value in metrics.items()
    }


def _variant(row: dict, observation: dict) -> EvidenceVariant:
    metrics = observation["metrics"]
    metadata = row.get("metadata") or {}
    window = observation["window"]
    utc_alignment = window.get("utc_alignment") or {}
    return EvidenceVariant(
        evidence_id=observation["evidence_id"],
        publication_id=row["publication_id"],
        channel=row["channel"],
        objective=metadata.get("objective"),
        language=row["language"],
        audience=metadata.get("audience_profile_key"),
        window_definition=window["definition"],
        window_start=date.fromisoformat(window["start"]),
        window_stop=date.fromisoformat(window["stop"]),
        impressions=metrics["impressions"] or 0,
        landing_page_views=metrics["meta_landing_page_views"] or 0,
        registrations=metrics["registrations"],
        metric_values=metrics,
        account_timezone=window.get("account_timezone"),
        utc_start=utc_alignment.get("start"),
        utc_stop_exclusive=utc_alignment.get("stop_exclusive"),
    )


def _comparison_key(variant: EvidenceVariant) -> tuple[Any, ...]:
    return (
        variant.channel,
        variant.objective,
        variant.language,
        variant.audience,
        variant.window_definition,
        variant.window_start,
        variant.window_stop,
        variant.account_timezone,
        variant.utc_start,
        variant.utc_stop_exclusive,
    )


async def _persist_learning(
    conn,
    variants: list[EvidenceVariant],
    thresholds: EvidenceThresholds,
    learning_type: str,
) -> None:
    decision = eligible_learning(variants, thresholds, learning_type=learning_type)
    if not decision.eligible:
        return
    exemplar = variants[0]
    scope = ":".join(
        (
            learning_type,
            exemplar.channel,
            exemplar.objective,
            exemplar.language,
            exemplar.audience,
            exemplar.window_definition,
            exemplar.account_timezone or "legacy-account-timezone",
        )
    )
    evidence = {
        "decision": {
            "eligible": decision.eligible,
            "reason": decision.reason,
            "learning_type": decision.learning_type,
            "metric": decision.metric,
            "outcome": decision.outcome,
        },
        "evidence_ids": list(decision.evidence_ids),
        "window": {
            "start": exemplar.window_start.isoformat(),
            "stop": exemplar.window_stop.isoformat(),
            "definition": exemplar.window_definition,
            "inclusive_days": (exemplar.window_stop - exemplar.window_start).days + 1,
        },
        "sample": decision.sample,
        "dimensions": {
            "channel": exemplar.channel,
            "objective": exemplar.objective,
            "language": exemplar.language,
            "audience": exemplar.audience,
            "window_definition": exemplar.window_definition,
            "account_timezone": exemplar.account_timezone,
            "utc_start": exemplar.utc_start,
            "utc_stop_exclusive": exemplar.utc_stop_exclusive,
        },
        "thresholds": {
            "impressions": thresholds.impressions,
            "landing_page_views": thresholds.landing_page_views,
            "registrations": thresholds.registrations,
        },
        "variants": [
            {
                "evidence_id": variant.evidence_id,
                "publication_id": variant.publication_id,
                "compared_values": variant.metric_values,
                "sample_sizes": {
                    "impressions": variant.impressions,
                    "meta_landing_page_views": variant.landing_page_views,
                    "registrations": variant.registrations,
                },
            }
            for variant in variants
        ],
    }
    evidence["decision_id"] = "|".join(sorted(decision.evidence_ids))
    existing = (
        (
            await conn.execute(
                text("SELECT id, evidence_links FROM learnings WHERE scope=:scope FOR UPDATE"),
                {"scope": scope},
            )
        )
        .mappings()
        .all()
    )
    for row in existing:
        links = dict(row["evidence_links"] or {})
        runs = list(links.get("runs") or [links])
        if any(run.get("decision_id") == evidence["decision_id"] for run in runs):
            return
        evidence["runs"] = [*runs, dict(evidence)]
        await conn.execute(
            text(
                "UPDATE learnings SET evidence_links=CAST(:evidence AS JSONB), "
                "seen_n_times=seen_n_times+1 WHERE id=:id"
            ),
            {"id": row["id"], "evidence": json.dumps(evidence)},
        )
        return
    evidence["runs"] = [dict(evidence)]
    await conn.execute(
        text(
            "INSERT INTO learnings (scope, text, evidence_links) "
            "VALUES (:scope, :learning, CAST(:evidence AS JSONB))"
        ),
        {
            "scope": scope,
            "learning": (
                f"{learning_type.title()} comparison: publication "
                f"#{decision.outcome['winner_publication_id']} outperformed publication "
                f"#{decision.outcome['loser_publication_id']} on {decision.metric} "
                f"({decision.outcome['winner_value']} vs {decision.outcome['loser_value']})."
            ),
            "evidence": json.dumps(evidence),
        },
    )


def _summary(rows: list[dict], observations: list[dict]) -> str:
    lines = ["Daily campaign evidence summary (descriptive observations only)"]
    by_id = {row["publication_id"]: row for row in rows}
    for observation in observations:
        row = by_id[observation["publication_id"]]
        metrics = observation["metrics"]
        display = lambda value: "unavailable" if value is None else str(value)  # noqa: E731
        window = observation["window"]
        line = (
            f"• Publication #{row['publication_id']} — "
            f"approved budget {display(metrics['approved_budget_cents'])} cents; "
            f"spend {display(metrics['spend_cents'])} cents; "
            f"delivery {display(metrics['delivery_state'])}; "
            f"impressions {display(metrics['impressions'])}; clicks {display(metrics['clicks'])}; "
            f"link clicks {display(metrics['link_clicks'])}; "
            f"Meta LPV {display(metrics['meta_landing_page_views'])}; "
            f"first-party landings {display(metrics['first_party_landing_views'])}; "
            f"attributed registrations {display(metrics['registrations'])}; "
            f"first listings created {display(metrics['first_listing_created'])}; "
            f"first listings published {display(metrics['first_listing_published'])}; "
            f"identity verifications {display(metrics['identity_verifications'])}; "
            f"cost/link click {display(metrics['cost_per_link_click_cents'])}; "
            f"click→Meta LPV {display(metrics['click_to_meta_landing'])}; "
            f"first-party landing→registration "
            f"{display(metrics['first_party_landing_to_registration'])}; "
            f"cost/registration {display(metrics['cost_per_registration_cents'])}; "
            f"registration→first listing {display(metrics['registration_to_first_listing'])}; "
            f"cost/first published listing "
            f"{display(metrics['cost_per_first_published_listing_cents'])}; "
            f"identity verification conversion "
            f"{display(metrics['identity_verification_conversion'])}; "
            f"attribution window {window['start']} → {window['stop']} UTC "
            f"({window['definition']}); sample sizes: impressions "
            f"{display(metrics['impressions'])}, Meta LPV "
            f"{display(metrics['meta_landing_page_views'])}, registrations "
            f"{display(metrics['registrations'])}"
        )
        if row.get("ads_manager_url"):
            line += f"; Ads Manager: {row['ads_manager_url']}"
        lines.append(line)
    return "\n".join(lines)


async def _enqueue_summary(conn, rows: list[dict], observations: list[dict]) -> None:
    """Persist immutable window summaries before any notification attempt."""
    grouped: dict[tuple[str, str, str], list[dict]] = {}
    for observation in observations:
        window = observation["window"]
        key = (window["start"], window["stop"], window["definition"])
        grouped.setdefault(key, []).append(observation)
    for (start, stop, definition), window_observations in grouped.items():
        ordered = sorted(window_observations, key=lambda item: item["publication_id"])
        publication_ids = [item["publication_id"] for item in ordered]
        evidence_ids = [item["evidence_id"] for item in ordered]
        identity = json.dumps(
            {
                "window": [start, stop, definition],
                "publication_ids": publication_ids,
                "evidence_ids": evidence_ids,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        summary_key = "daily-performance:" + sha256(identity.encode()).hexdigest()
        await conn.execute(
            text(
                "INSERT INTO daily_performance_summary_outbox "
                "(summary_key, window_start, window_stop, window_definition, "
                "publication_ids, evidence_ids, message) VALUES "
                "(:key, :start, :stop, :definition, CAST(:publication_ids AS JSONB), "
                "CAST(:evidence_ids AS JSONB), :message) "
                "ON CONFLICT (summary_key) DO NOTHING"
            ),
            {
                "key": summary_key,
                "start": date.fromisoformat(start),
                "stop": date.fromisoformat(stop),
                "definition": definition,
                "publication_ids": json.dumps(publication_ids),
                "evidence_ids": json.dumps(evidence_ids),
                "message": _summary(rows, ordered),
            },
        )


async def _enqueue_unavailable_diagnostics(conn, rows: list[dict], run_day: date) -> None:
    """Persist one non-evidence diagnostic per publication and UTC run day."""
    for row in rows:
        latest = (row["performance"].get("meta") or {}).get("latest") or {}
        if _valid_window(latest) is not None:
            continue
        publication_id = row["publication_id"]
        await conn.execute(
            text(
                "INSERT INTO daily_performance_summary_outbox "
                "(summary_key, summary_kind, run_day, publication_ids, evidence_ids, message) "
                "VALUES (:key, 'source_window_unavailable', :run_day, "
                "CAST(:publication_ids AS JSONB), '[]'::JSONB, :message) "
                "ON CONFLICT (summary_key) DO NOTHING"
            ),
            {
                "key": f"daily-performance-unavailable:{publication_id}:{run_day.isoformat()}",
                "run_day": run_day,
                "publication_ids": json.dumps([publication_id]),
                "message": f"Publication #{publication_id} — source window unavailable",
            },
        )


async def _claim_next_summary(engine: AsyncEngine, now: datetime) -> tuple[str, str] | None:
    """Claim only the oldest pending summary so newer windows cannot overtake it."""
    async with engine.begin() as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT id, message, claim_token, claim_expires_at "
                        "FROM daily_performance_summary_outbox WHERE status='pending' "
                        "ORDER BY id LIMIT 1 FOR UPDATE"
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        if row["claim_token"] and row["claim_expires_at"] and row["claim_expires_at"] > now:
            return None
        token = str(uuid4())
        await conn.execute(
            text(
                "UPDATE daily_performance_summary_outbox SET claim_token=:token, "
                "claim_expires_at=:expires, attempt_count=attempt_count+1, "
                "last_attempt_at=:now, last_failure=NULL WHERE id=:id"
            ),
            {
                "id": row["id"],
                "token": token,
                "expires": now + _SUMMARY_CLAIM_LEASE,
                "now": now,
            },
        )
    return token, row["message"]


async def _finish_summary(
    engine: AsyncEngine,
    token: str,
    *,
    delivered: bool,
    failure: str | None,
    now: datetime,
) -> None:
    async with engine.begin() as conn:
        if delivered:
            await conn.execute(
                text(
                    "UPDATE daily_performance_summary_outbox SET status='sent', sent_at=:now, "
                    "claim_token=NULL, claim_expires_at=NULL, last_failure=NULL "
                    "WHERE status='pending' AND claim_token=:token"
                ),
                {"token": token, "now": now},
            )
        else:
            await conn.execute(
                text(
                    "UPDATE daily_performance_summary_outbox SET claim_token=NULL, "
                    "claim_expires_at=NULL, last_failure=:failure "
                    "WHERE status='pending' AND claim_token=:token"
                ),
                {"token": token, "failure": failure},
            )


async def _drain_summaries(engine: AsyncEngine, notifier, now: datetime) -> None:
    while claimed := await _claim_next_summary(engine, now):
        token, message = claimed
        failure = None
        try:
            delivered = bool(await notifier.notify_founder(message))
            if not delivered:
                failure = "notification_not_confirmed"
        except Exception:
            delivered = False
            failure = "notification_exception"
        await _finish_summary(
            engine,
            token,
            delivered=delivered,
            failure=failure,
            now=now,
        )
        if not delivered:
            log.warning("daily_performance.summary_pending", failure=failure)
            return


async def run_daily_performance(
    engine: AsyncEngine, notifier, settings, now: datetime | None = None
) -> int:
    """Append one observation per publication/window and summarize without mutation."""
    now = now or datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    inserted: list[dict] = []
    rows: list[dict] = []
    async with engine.begin() as conn:
        db_rows = (
            (
                await conn.execute(
                    text(
                        "SELECT p.id AS publication_id, p.channel, p.ads_manager_url, "
                        "p.approved_budget_cents, p.performance, d.language, d.metadata "
                        "FROM publications p JOIN drafts d ON d.id=p.draft_id "
                        "WHERE p.channel='meta' ORDER BY p.id FOR UPDATE OF p"
                    )
                )
            )
            .mappings()
            .all()
        )
        for raw_row in db_rows:
            row = dict(raw_row)
            performance = dict(row["performance"] or {})
            performance["approved_budget_cents"] = row["approved_budget_cents"]
            latest = (performance.get("meta") or {}).get("latest") or {}
            window = _valid_window(latest)
            if window is None:
                row["performance"] = performance
                rows.append(row)
                continue
            start, stop, definition = window
            account_timezone = latest.get("account_timezone")
            utc_alignment = latest.get("utc_alignment")
            if isinstance(account_timezone, str) and isinstance(utc_alignment, dict):
                if stop >= now.astimezone(ZoneInfo(account_timezone)).date():
                    row["performance"] = performance
                    rows.append(row)
                    continue
            else:
                # Upgrade compatibility for snapshots frozen before account-window
                # metadata existed. New hourly snapshots always take the strict path.
                account_timezone = getattr(settings, "meta_account_timezone", "Europe/Brussels")
                zone = ZoneInfo(account_timezone)
                utc_start = datetime.combine(start, datetime.min.time(), zone).astimezone(UTC)
                utc_stop = datetime.combine(
                    stop + timedelta(days=1), datetime.min.time(), zone
                ).astimezone(UTC)
                utc_alignment = {
                    "start": utc_start.isoformat(),
                    "stop_exclusive": utc_stop.isoformat(),
                    "overlap_start_day": utc_start.date().isoformat(),
                    "overlap_stop_day": (utc_stop - timedelta(microseconds=1)).date().isoformat(),
                    "source": "derived_for_legacy_snapshot",
                }
            if not utc_alignment.get("start") or not utc_alignment.get("stop_exclusive"):
                row["performance"] = performance
                rows.append(row)
                continue
            evidence_id = (
                f"publication:{row['publication_id']}:{start.isoformat()}:{stop.isoformat()}:"
                f"{definition}:{account_timezone}:{utc_alignment.get('start')}:"
                f"{utc_alignment.get('stop_exclusive')}"
            )
            observations = list(performance.get("daily_observations") or [])
            observation = next(
                (item for item in observations if item.get("evidence_id") == evidence_id), None
            )
            if observation is None:
                observation = {
                    "evidence_id": evidence_id,
                    "publication_id": row["publication_id"],
                    "observed_at": now.astimezone(UTC).isoformat(),
                    "window": {
                        "start": start.isoformat(),
                        "stop": stop.isoformat(),
                        "definition": definition,
                        "account_timezone": account_timezone,
                        "utc_alignment": utc_alignment,
                    },
                    "metrics": _json_metrics(evaluate_publication(performance).metrics),
                }
                observations.append(observation)
                performance["daily_observations"] = observations
                await conn.execute(
                    text(
                        "UPDATE publications SET performance=CAST(:performance AS JSONB), "
                        "updated_at=NOW() WHERE id=:id"
                    ),
                    {"id": row["publication_id"], "performance": json.dumps(performance)},
                )
                inserted.append(observation)
            row["performance"] = performance
            rows.append(row)

        grouped: dict[tuple[Any, ...], list[EvidenceVariant]] = {}
        for row in rows:
            for observation in row["performance"].get("daily_observations", []):
                variant = _variant(row, observation)
                grouped.setdefault(_comparison_key(variant), []).append(variant)
        thresholds = EvidenceThresholds(
            impressions=getattr(
                settings, "learning_min_impressions", DEFAULT_THRESHOLDS.impressions
            ),
            landing_page_views=getattr(
                settings,
                "learning_min_landing_page_views",
                DEFAULT_THRESHOLDS.landing_page_views,
            ),
            registrations=getattr(
                settings, "learning_min_registrations", DEFAULT_THRESHOLDS.registrations
            ),
        )
        for variants in grouped.values():
            await _persist_learning(conn, variants, thresholds, "delivery")
            await _persist_learning(conn, variants, thresholds, "conversion")
        all_observations = [
            observation
            for row in rows
            for observation in row["performance"].get("daily_observations", [])
        ]
        await _enqueue_summary(conn, rows, all_observations)
        await _enqueue_unavailable_diagnostics(conn, rows, now.astimezone(UTC).date())

    await _drain_summaries(engine, notifier, now)
    log.info("daily_performance.complete", observations_inserted=len(inserted))
    return len(inserted)
