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
    metrics: dict[str, int | Decimal | None]


def safe_ratio(numerator: int | None, denominator: int | None) -> Decimal | None:
    if numerator is None or denominator in (None, 0):
        return None
    return Decimal(numerator) / Decimal(denominator)


def evaluate_publication(performance: dict) -> EvidenceObservation:
    """Derive ratios without converting absent attribution into a zero."""
    latest = (performance.get("meta") or {}).get("latest") or performance.get("meta") or {}
    attribution = performance.get("attribution") or {}
    registrations = None
    if attribution.get("available") is True:
        registrations = sum(
            _count(event.get("event_count"))
            for event in attribution.get("events", [])
            if isinstance(event, dict) and event.get("event_type") == "registration"
        )
    impressions = _optional_count(latest.get("impressions"))
    landing_page_views = _optional_count(latest.get("landing_page_views"))
    return EvidenceObservation(
        metrics={
            "impressions": impressions,
            "landing_page_views": landing_page_views,
            "registrations": registrations,
            "impression_to_landing": safe_ratio(landing_page_views, impressions),
            "landing_to_registration": safe_ratio(registrations, landing_page_views),
        }
    )


def _count(value: object) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _optional_count(value: object) -> int | None:
    return (
        _count(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    )


def _date(value: object, fallback: date) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return fallback


def _json_metrics(metrics: dict[str, int | Decimal | None]) -> dict[str, int | str | None]:
    return {
        name: str(value) if isinstance(value, Decimal) else value for name, value in metrics.items()
    }


def _variant(row: dict, observation: dict) -> EvidenceVariant:
    metrics = observation["metrics"]
    metadata = row.get("metadata") or {}
    return EvidenceVariant(
        evidence_id=observation["evidence_id"],
        channel=row["channel"],
        objective=str(metadata.get("objective") or "OUTCOME_TRAFFIC"),
        language=row["language"],
        audience=str(metadata.get("audience_profile_key") or "unspecified"),
        window_definition=observation["window"]["definition"],
        window_start=date.fromisoformat(observation["window"]["start"]),
        window_stop=date.fromisoformat(observation["window"]["stop"]),
        impressions=metrics["impressions"] or 0,
        landing_page_views=metrics["landing_page_views"] or 0,
        registrations=metrics["registrations"],
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
        "evidence_ids": list(decision.evidence_ids),
        "window": {
            "start": exemplar.window_start.isoformat(),
            "stop": exemplar.window_stop.isoformat(),
            "definition": exemplar.window_definition,
        },
        "sample": decision.sample,
    }
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
    evidence_set = set(decision.evidence_ids)
    for row in existing:
        links = dict(row["evidence_links"] or {})
        prior_sets = [set(links.get("evidence_ids") or [])]
        prior_sets.extend(set(item) for item in links.get("prior_evidence_ids", []))
        if evidence_set in prior_sets:
            return
        prior = list(links.get("prior_evidence_ids", []))
        if links.get("evidence_ids"):
            prior.append(links["evidence_ids"])
        evidence["prior_evidence_ids"] = prior
        await conn.execute(
            text(
                "UPDATE learnings SET evidence_links=CAST(:evidence AS JSONB), "
                "seen_n_times=seen_n_times+1 WHERE id=:id"
            ),
            {"id": row["id"], "evidence": json.dumps(evidence)},
        )
        return
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
            f"impressions {display(metrics['impressions'])}; "
            f"Meta LPV {display(metrics['landing_page_views'])}; "
            f"attributed registrations {display(metrics['registrations'])}; "
            f"LPV→registration {display(metrics['landing_to_registration'])}; "
            f"attribution window {window['start']} → {window['stop']} UTC"
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
                        "p.performance, d.language, d.metadata "
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
            latest = (performance.get("meta") or {}).get("latest") or {}
            start = _date(latest.get("window_start"), today)
            stop = _date(latest.get("window_stop"), today)
            evidence_id = (
                f"publication:{row['publication_id']}:{start.isoformat()}:{stop.isoformat()}"
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
                        "definition": "utc-day",
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

    await notifier.notify_founder(
        _summary(
            rows,
            inserted
            or [
                observation
                for row in rows
                for observation in row["performance"].get("daily_observations", [])
                if observation["window"]["stop"] == today.isoformat()
            ],
        )
    )
    log.info("daily_performance.complete", observations_inserted=len(inserted))
    return len(inserted)
