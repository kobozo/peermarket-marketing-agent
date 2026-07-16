"""Daily, observation-only summaries of attributed campaign evidence."""

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from peermarket_agent.learnings import (
    DEFAULT_THRESHOLDS,
    EvidenceVariant,
    eligible_learning,
)

log = structlog.get_logger(__name__)


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
    return EvidenceVariant(
        evidence_id=observation["evidence_id"],
        publication_id=row["publication_id"],
        channel=row["channel"],
        objective=metadata.get("objective"),
        language=row["language"],
        audience=metadata.get("audience_profile_key"),
        window_definition=observation["window"]["definition"],
        window_start=date.fromisoformat(observation["window"]["start"]),
        window_stop=date.fromisoformat(observation["window"]["stop"]),
        impressions=metrics["impressions"] or 0,
        landing_page_views=metrics["meta_landing_page_views"] or 0,
        registrations=metrics["registrations"],
        metric_values=metrics,
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
    )


async def _persist_learning(conn, variants: list[EvidenceVariant]) -> None:
    decision = eligible_learning(variants, DEFAULT_THRESHOLDS)
    if not decision.eligible:
        return
    exemplar = variants[0]
    scope = ":".join(
        (
            exemplar.channel,
            exemplar.objective,
            exemplar.language,
            exemplar.audience,
            exemplar.window_definition,
        )
    )
    evidence = {
        "decision": {"eligible": decision.eligible, "reason": decision.reason},
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
        },
        "thresholds": {
            "impressions": DEFAULT_THRESHOLDS.impressions,
            "landing_page_views": DEFAULT_THRESHOLDS.landing_page_views,
            "registrations": DEFAULT_THRESHOLDS.registrations,
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
            "learning": "Comparable variants met the minimum delivery and attributed-registration evidence thresholds.",
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


async def run_daily_performance(
    engine: AsyncEngine, notifier, settings, now: datetime | None = None
) -> int:
    """Append one observation per publication/window and summarize without mutation."""
    del settings  # reserved for future presentation settings; never used to mutate Meta
    now = now or datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    today = now.astimezone(UTC).date()
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
            evidence_id = (
                f"publication:{row['publication_id']}:{start.isoformat()}:{stop.isoformat()}:"
                f"{definition}"
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
        for variants in grouped.values():
            await _persist_learning(conn, variants)

    summary_observations = inserted or [
        observation
        for row in rows
        for observation in row["performance"].get("daily_observations", [])
        if observation["window"]["stop"] == today.isoformat()
    ]
    message = _summary(rows, summary_observations)
    unavailable_rows = [
        row
        for row in rows
        if _valid_window((row["performance"].get("meta") or {}).get("latest") or {}) is None
    ]
    for row in unavailable_rows:
        message += f"\n• Publication #{row['publication_id']} — source window unavailable"
        if row.get("ads_manager_url"):
            message += f"; Ads Manager: {row['ads_manager_url']}"
    await notifier.notify_founder(message)
    log.info("daily_performance.complete", observations_inserted=len(inserted))
    return len(inserted)
