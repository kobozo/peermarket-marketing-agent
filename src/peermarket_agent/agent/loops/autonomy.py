"""Evaluate and execute the autonomous Meta lifecycle after collection."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from peermarket_agent.autonomy.contracts import DecisionKind, FrozenDecision
from peermarket_agent.autonomy.executor import execute_production_claim
from peermarket_agent.autonomy.snapshot import build_policy_decision
from peermarket_agent.autonomy.store import (
    _sanitize_audit_value,
    campaign_history,
    claim_next_action,
    enqueue_action,
    record_decision,
)
from peermarket_agent.slack_outbox import deliver_pending_outbox

log = structlog.get_logger(__name__)
_EXECUTABLE = {
    DecisionKind.PAUSE,
    DecisionKind.REPLACE,
    DecisionKind.REALLOCATE,
    DecisionKind.SCALE,
}


def _registrations(performance: dict) -> int:
    events = (performance.get("attribution") or {}).get("events") or ()
    return sum(
        int(item.get("event_count", item.get("count", 0)) or 0)
        for item in events
        if isinstance(item, dict)
        and str(item.get("event_type", "")).casefold() in {"registration", "signup", "sign_up"}
    )


def _variant(row: dict) -> dict:
    performance = dict(row.get("performance") or {})
    latest = (performance.get("meta") or {}).get("latest") or {}
    metadata = dict(row.get("metadata") or {})
    alignment = latest.get("utc_alignment") or {}
    return {
        "variant_id": str(row["draft_id"]),
        "publication_id": int(row["publication_id"]),
        "channel": "meta",
        "objective": metadata.get("objective") or "OUTCOME_TRAFFIC",
        "language": row.get("language") or "MULTI",
        "audience": metadata.get("audience_profile_key") or "unknown",
        "creative_dimension": metadata.get("changed_dimension") or "baseline",
        "window_definition": latest.get("window_definition")
        or f"utc:{alignment.get('start')}:{alignment.get('stop_exclusive')}",
        "impressions": int(latest.get("impressions") or 0),
        "landing_page_views": int(latest.get("landing_page_views") or 0),
        "registrations": _registrations(performance),
    }


def _lineage(row: dict) -> tuple:
    metadata = dict(row.get("metadata") or {})
    variant = _variant(row)
    return (
        metadata.get("experiment_id") or f"draft:{row['draft_id']}",
        variant["channel"],
        variant["objective"],
        variant["language"],
        variant["audience"],
        variant["creative_dimension"],
        variant["window_definition"],
    )


def _replacement_source(row: dict) -> dict | None:
    metadata = dict(row.get("metadata") or {})
    ids = dict(row.get("external_ids") or {})
    locales = metadata.get("locales")
    ad_ids = ids.get("ad_ids")
    creative_ids = ids.get("creative_ids")
    required = (
        metadata.get("experiment_id"),
        metadata.get("changed_dimension"),
        metadata.get("audience_profile_key"),
        metadata.get("image_prompt"),
        metadata.get("asset_path"),
        metadata.get("landing_page_url"),
        ids.get("campaign_id"),
        ids.get("ad_set_id"),
    )
    if (
        not all(required)
        or not isinstance(locales, dict)
        or set(locales) != {"NL", "FR", "EN"}
        or not isinstance(ad_ids, dict)
        or set(ad_ids) != {"NL", "FR", "EN"}
        or not isinstance(creative_ids, dict)
        or set(creative_ids) != {"NL", "FR", "EN"}
    ):
        return None
    budget = row.get("approved_budget_cents")
    if type(budget) is not int or budget % 100 or not 500 <= budget <= 2000:
        return None
    return {
        "draft_id": int(row["draft_id"]),
        "publication_id": int(row["publication_id"]),
        "campaign_id": ids["campaign_id"],
        "experiment_id": metadata["experiment_id"],
        "changed_dimension": metadata["changed_dimension"],
        "locales": locales,
        "audience_profile_key": metadata["audience_profile_key"],
        "image_prompt": metadata["image_prompt"],
        "asset_path": metadata["asset_path"],
        "daily_budget_eur": budget // 100,
        "landing_page_url": metadata["landing_page_url"],
        "objective": metadata.get("objective") or "OUTCOME_TRAFFIC",
        "current_meta_ids": {
            "campaign_id": ids["campaign_id"],
            "ad_set_id": ids["ad_set_id"],
            "ad_ids": ad_ids,
            "creative_ids": creative_ids,
        },
    }


def _allocation(variant_rows: list[dict]) -> dict | None:
    if len(variant_rows) < 2:
        return None
    ordered = sorted(
        variant_rows,
        key=lambda row: (
            _variant(row)["registrations"] / max(1, _variant(row)["landing_page_views"]),
            str(row["draft_id"]),
        ),
    )
    loser, winner = ordered[0], ordered[-1]
    loser_ids, winner_ids = loser["external_ids"], winner["external_ids"]
    if any(
        not ids.get(key)
        for ids in (loser_ids, winner_ids)
        for key in ("campaign_id", "ad_set_id", "ad_id")
    ):
        return None
    if loser_ids["campaign_id"] != winner_ids["campaign_id"]:
        return None
    total = int(loser["approved_budget_cents"]) + int(winner["approved_budget_cents"])
    loser_new = total * 40 // 100
    winner_new = total - loser_new
    if not (
        loser_new < int(loser["approved_budget_cents"])
        and winner_new > int(winner["approved_budget_cents"])
    ):
        return None
    return {
        "old_budget_cents": total,
        "new_budget_cents": total,
        "allocations": {
            "loser": {
                "campaign_id": loser_ids["campaign_id"],
                "variant_id": str(loser["draft_id"]),
                "ad_set_id": loser_ids["ad_set_id"],
                "ad_id": loser_ids["ad_id"],
                "old_budget_cents": int(loser["approved_budget_cents"]),
                "new_budget_cents": loser_new,
            },
            "winner": {
                "campaign_id": winner_ids["campaign_id"],
                "variant_id": str(winner["draft_id"]),
                "ad_set_id": winner_ids["ad_set_id"],
                "ad_id": winner_ids["ad_id"],
                "old_budget_cents": int(winner["approved_budget_cents"]),
                "new_budget_cents": winner_new,
            },
        },
    }


def _canonical_inputs(row: dict, rows: list[dict]) -> dict:
    comparable = [item for item in rows if _lineage(item) == _lineage(row)]
    return {
        "schema": "autonomy-inputs/v1",
        "variants": [_variant(item) for item in comparable],
        "replacement_source": _replacement_source(row),
        "reallocation": _allocation(comparable),
    }


def _setting(settings: Any, name: str, default: Any = None) -> Any:
    return getattr(settings, name, default)


async def _eligible_campaigns(engine: AsyncEngine, settings: Any, now: datetime) -> list[dict]:
    allowlist = tuple(_setting(settings, "meta_autonomy_campaign_ids", ()))
    if not allowlist:
        return []
    async with engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT p.id AS publication_id,p.draft_id,p.external_ids,"
                        "p.approved_budget_cents,p.performance,d.metadata,d.language FROM publications p "
                        "JOIN drafts d ON d.id=p.draft_id WHERE p.channel='meta' "
                        "AND p.state IN ('active','published') "
                        "AND p.external_ids->>'campaign_id'=ANY(:campaigns) "
                        "ORDER BY p.draft_id"
                    ),
                    {"campaigns": list(allowlist)},
                )
            )
            .mappings()
            .all()
        )
    eligible = []
    plain_rows = [dict(row) for row in rows]
    grouped: dict[str, list[dict]] = {}
    for item in plain_rows:
        campaign = str((item.get("external_ids") or {}).get("campaign_id") or "")
        grouped.setdefault(campaign, []).append(item)
    for campaign_id in sorted(grouped):
        campaign_rows = sorted(
            grouped[campaign_id], key=lambda item: (item["draft_id"], item["publication_id"])
        )
        row = campaign_rows[0]
        try:
            performance = dict(row["performance"] or {})
            if not isinstance(performance.get("autonomy_basis"), dict):
                continue
            inputs = performance.get("autonomy_inputs")
            if not isinstance(inputs, dict) or inputs.get("schema") != "autonomy-inputs/v1":
                inputs = _canonical_inputs(row, campaign_rows)
            variants = inputs["variants"]
            source = inputs["replacement_source"]
            publications = [
                {
                    "publication_id": int(item["publication_id"]),
                    "draft_id": int(item["draft_id"]),
                    "approved_budget_cents": int(item["approved_budget_cents"]),
                    "external_ids": dict(item["external_ids"] or {}),
                }
                for item in campaign_rows
            ]
            basis = dict(performance["autonomy_basis"])
            campaign_total = sum(item["approved_budget_cents"] for item in publications)
            touched_budget = (
                inputs["reallocation"]["old_budget_cents"]
                if inputs.get("reallocation") is not None
                else campaign_total
            )
            basis.update(
                {
                    "approved_budget_cents": touched_budget,
                    "publication_ids": [item["publication_id"] for item in publications],
                    "campaign_publications": publications,
                    "campaign_total_budget_cents": campaign_total,
                    "touched_allocation_budget_cents": touched_budget,
                }
            )
            performance["autonomy_basis"] = basis
            publication = {
                "external_ids": dict(row["external_ids"] or {}),
                "approved_budget_cents": touched_budget,
                "performance": performance,
            }
            history_rows = await campaign_history(engine, campaign_id)
            history = []
            for item in history_rows:
                if item.get("status") == "succeeded":
                    history.append(
                        {
                            "event_id": f"action:{item['id']}",
                            "kind": item["kind"],
                            "at": item["updated_at"],
                        }
                    )
                for event in item.get("budget_events", ()):
                    history.append(
                        {
                            "event_id": f"budget:{event['id']}",
                            "kind": "budget",
                            "at": event["created_at"],
                            "old_budget_cents": event["old_budget_cents"],
                            "new_budget_cents": event["new_budget_cents"],
                        }
                    )
            decision = build_policy_decision(
                publication,
                variants,
                replacement_source=source,
                history=history,
                limits=settings,
                now=now,
                allow_replacement=source is not None,
                reallocation=inputs["reallocation"],
            )
            eligible.append({"draft_id": row["draft_id"], "decision": decision})
        except Exception:
            log.exception("autonomy.campaign_snapshot_failed", draft_id=row["draft_id"])
            campaign_id = str((row.get("external_ids") or {}).get("campaign_id") or "")
            if campaign_id.isascii() and campaign_id.isdecimal():
                eligible.append(
                    {
                        "draft_id": row["draft_id"],
                        "decision": FrozenDecision(
                            DecisionKind.OBSERVE,
                            campaign_id,
                            {
                                "snapshot_id": f"unavailable:{row['publication_id']}:{now.isoformat()}",
                                "availability": "canonical_inputs_unavailable",
                            },
                            "canonical_inputs_unavailable",
                            now - timedelta(microseconds=1),
                            now,
                            f"unavailable:{row['publication_id']}:{now.isoformat()}",
                        ),
                    }
                )
    return eligible


async def persist_autonomy_inputs(engine: AsyncEngine) -> int:
    """Persist the exact canonical producer schema consumed by the autonomy cycle."""
    async with engine.begin() as conn:
        rows = [
            dict(row)
            for row in (
                (
                    await conn.execute(
                        text(
                            "SELECT p.id AS publication_id,p.draft_id,p.external_ids,"
                            "p.approved_budget_cents,p.performance,d.metadata,d.language "
                            "FROM publications p JOIN drafts d ON d.id=p.draft_id "
                            "WHERE p.channel='meta' AND p.state IN ('active','published') "
                            "ORDER BY p.draft_id"
                        )
                    )
                )
                .mappings()
                .all()
            )
        ]
        updated = 0
        for row in rows:
            performance = dict(row.get("performance") or {})
            if not isinstance(performance.get("autonomy_basis"), dict):
                continue
            campaign = (row.get("external_ids") or {}).get("campaign_id")
            campaign_rows = [
                item
                for item in rows
                if (item.get("external_ids") or {}).get("campaign_id") == campaign
            ]
            performance["autonomy_inputs"] = _canonical_inputs(row, campaign_rows)
            result = await conn.execute(
                text(
                    "UPDATE publications SET performance=CAST(:performance AS JSONB),updated_at=NOW() "
                    "WHERE id=:id"
                ),
                {
                    "id": row["publication_id"],
                    "performance": json.dumps(performance, default=str),
                },
            )
            updated += result.rowcount
    return updated


async def _audit(
    engine: AsyncEngine,
    *,
    draft_id: int,
    decision: Any,
    outcome: str,
    detail: str,
    rollback_result: Any = None,
    next_evaluation_at: datetime | None = None,
) -> None:
    """Persist a sanitized immutable Slack payload for retry by the outbox worker."""
    key = f"autonomy:{decision.idempotency_key}:{outcome}"
    thresholds = dict(decision.evidence.get("policy_limits") or {})
    evidence = [
        {
            key: item.get(key)
            for key in ("variant_id", "impressions", "landing_page_views", "registrations")
        }
        for item in decision.evidence.get("variants", ())
    ]
    allocations = decision.allocations or {}
    affected_ads = [
        {
            "publication_id": item.get("publication_id"),
            "ad_set_id": item.get("ad_set_id"),
            "ad_id": item.get("ad_id"),
        }
        for item in allocations.values()
    ]
    if not affected_ads:
        affected_ads = [
            {
                "publication_id": item.get("publication_id"),
                "ad_set_id": (item.get("external_ids") or {}).get("ad_set_id"),
                "ad_id": (item.get("external_ids") or {}).get("ad_id"),
            }
            for item in (decision.evidence.get("frozen_basis") or {}).get(
                "campaign_publications", ()
            )
        ]
    affected_ads.sort(key=lambda item: (item.get("publication_id") or 0, item.get("ad_id") or ""))
    rollback = _sanitize_audit_value(rollback_result or {"needed": False, "result": "not_required"})
    if next_evaluation_at is None:
        next_evaluation_at = decision.window_end + timedelta(
            hours=int(thresholds.get("cooldown_hours", 24))
        )
    next_evaluation = next_evaluation_at.isoformat() if next_evaluation_at else None
    payload = {
        "audit": "autonomy",
        "campaign_id": decision.campaign_id,
        "outcome": outcome,
        "decision": decision.kind.value,
        "reason": decision.reason,
        "thresholds": thresholds,
        "evidence": evidence,
        "affected_ads": affected_ads,
        "budgets": {
            "previous_cents": decision.old_budget_cents,
            "new_cents": decision.new_budget_cents,
        },
        "rollback": rollback,
        "next_evaluation_at": next_evaluation,
        "text": (
            f"Autonomy {outcome}: campaign {decision.campaign_id}; "
            f"decision {decision.kind.value}; reason {decision.reason}; "
            f"thresholds {json.dumps(thresholds, sort_keys=True)}; "
            f"samples {json.dumps(evidence, sort_keys=True)}; "
            f"affected ads {json.dumps(affected_ads, sort_keys=True)}; "
            f"budget {decision.old_budget_cents}->{decision.new_budget_cents}; "
            f"rollback {json.dumps(rollback, sort_keys=True)}; "
            f"next evaluation {next_evaluation or 'pending'}; "
            f"detail {detail}"
        ),
    }
    async with engine.begin() as conn:
        await conn.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:campaign, 7))"),
            {"campaign": decision.campaign_id},
        )
        await conn.execute(
            text(
                "UPDATE slack_outbox SET status='obsolete',lease_owner=NULL,lease_expires_at=NULL,"
                "last_failure_category='superseded_autonomy_lifecycle' "
                "WHERE autonomy_campaign_id=:campaign AND message_kind='autonomy_audit' "
                "AND status IN ('pending','failed') AND idempotency_key<>:key"
            ),
            {"campaign": decision.campaign_id, "key": key},
        )
        await conn.execute(
            text(
                "INSERT INTO slack_outbox(idempotency_key,draft_id,message_kind,payload,autonomy_campaign_id) "
                "VALUES (:key,:draft,'autonomy_audit',CAST(:payload AS JSONB),:campaign) "
                "ON CONFLICT (idempotency_key) DO NOTHING"
            ),
            {
                "key": key,
                "draft": draft_id,
                "campaign": decision.campaign_id,
                "payload": json.dumps(payload),
            },
        )


async def run_autonomy_cycle(
    engine: AsyncEngine, claude: Any, notifier: Any, settings: Any, now: datetime | None = None
) -> dict[str, int]:
    """Persist policy decisions and execute a bounded, campaign-isolated work cycle."""
    summary = {"evaluated": 0, "queued": 0, "executed": 0, "failed": 0}
    if not _setting(settings, "meta_autonomy_enabled", False):
        return summary
    now = now or datetime.now(UTC)
    try:
        candidates = await _eligible_campaigns(engine, settings, now)
    except Exception:
        log.exception("autonomy.load_failed")
        summary["failed"] += 1
        return summary
    draft_by_campaign: dict[str, int] = {}
    for candidate in candidates:
        decision = candidate["decision"]
        draft_id = int(candidate["draft_id"])
        draft_by_campaign[decision.campaign_id] = draft_id
        try:
            await record_decision(engine, decision)
            summary["evaluated"] += 1
            if _setting(settings, "meta_autonomy_shadow", True):
                await _audit(
                    engine,
                    draft_id=draft_id,
                    decision=decision,
                    outcome="shadow",
                    detail="decision persisted; no action queued",
                )
            elif decision.kind in _EXECUTABLE:
                queued = await enqueue_action(engine, decision)
                summary["queued"] += int(queued.created)
            else:
                await _audit(
                    engine,
                    draft_id=draft_id,
                    decision=decision,
                    outcome="observe",
                    detail="next evaluation retained",
                )
        except Exception as error:
            summary["failed"] += 1
            log.exception("autonomy.campaign_evaluation_failed", campaign_id=decision.campaign_id)
            with_exception = f"{type(error).__name__}"
            try:
                await _audit(
                    engine,
                    draft_id=draft_id,
                    decision=decision,
                    outcome="failure",
                    detail=with_exception,
                )
            except Exception:
                log.exception("autonomy.audit_failed", campaign_id=decision.campaign_id)
    if _setting(settings, "meta_autonomy_shadow", True):
        if notifier is not None:
            try:
                await deliver_pending_outbox(engine, notifier)
            except Exception:
                log.exception("autonomy.audit_delivery_failed")
        return summary
    limit = min(len(draft_by_campaign), int(_setting(settings, "meta_autonomy_cycle_limit", 50)))
    worker = f"autonomy-{uuid.uuid4().hex}"
    for _ in range(limit):
        claim = await claim_next_action(engine, worker)
        if claim is None:
            break
        draft_id = draft_by_campaign.get(claim.campaign_id)
        try:
            result = await execute_production_claim(engine, settings, claude, claim, now)
            summary["executed"] += 1
            outcome = result.status.value
            detail_parts = [result.reason]
            if result.rollback_result is not None:
                detail_parts.append("rollback recorded")
            if result.retry_at is not None:
                detail_parts.append(f"next evaluation {result.retry_at.isoformat()}")
            detail = "; ".join(detail_parts)
            if draft_id is not None:
                await _audit(
                    engine,
                    draft_id=draft_id,
                    decision=claim.decision,
                    outcome=outcome,
                    detail=detail,
                    rollback_result=(
                        result.rollback_result
                        if result.rollback_result is not None
                        else {"needed": False, "result": "not_required"}
                    ),
                    next_evaluation_at=result.retry_at,
                )
        except Exception as error:
            summary["failed"] += 1
            log.exception("autonomy.execution_failed", campaign_id=claim.campaign_id)
            if draft_id is not None:
                try:
                    await _audit(
                        engine,
                        draft_id=draft_id,
                        decision=claim.decision,
                        outcome="failure",
                        detail=type(error).__name__,
                    )
                except Exception:
                    log.exception("autonomy.audit_failed", campaign_id=claim.campaign_id)
    if notifier is not None:
        try:
            await deliver_pending_outbox(engine, notifier)
        except Exception:
            log.exception("autonomy.audit_delivery_failed")
    return summary
