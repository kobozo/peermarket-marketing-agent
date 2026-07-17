"""Evaluate and execute the autonomous Meta lifecycle after collection."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from peermarket_agent.autonomy.contracts import DecisionKind
from peermarket_agent.autonomy.executor import execute_production_claim
from peermarket_agent.autonomy.snapshot import build_policy_decision
from peermarket_agent.autonomy.store import (
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
                        "p.approved_budget_cents,p.performance,d.metadata FROM publications p "
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
    for row in rows:
        try:
            performance = dict(row["performance"] or {})
            if not isinstance(performance.get("autonomy_basis"), dict):
                continue
            variants = performance.get("autonomy_variants") or performance.get("variants") or []
            source = performance.get("replacement_source")
            publication = {
                "external_ids": dict(row["external_ids"] or {}),
                "approved_budget_cents": row["approved_budget_cents"],
                "performance": performance,
            }
            campaign_id = publication["external_ids"]["campaign_id"]
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
                reallocation=performance.get("reallocation"),
            )
            eligible.append({"draft_id": row["draft_id"], "decision": decision})
        except Exception:
            log.exception("autonomy.campaign_snapshot_failed", draft_id=row["draft_id"])
    return eligible


async def _audit(
    engine: AsyncEngine,
    *,
    draft_id: int,
    decision: Any,
    outcome: str,
    detail: str,
) -> None:
    """Persist a sanitized immutable Slack payload for retry by the outbox worker."""
    key = f"autonomy:{decision.idempotency_key}:{outcome}"
    payload = {
        "audit": "autonomy",
        "text": (
            f"Autonomy {outcome}: campaign {decision.campaign_id}; "
            f"decision {decision.kind.value}; reason {decision.reason}; detail {detail}"
        ),
    }
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO slack_outbox(idempotency_key,draft_id,message_kind,payload) "
                "VALUES (:key,:draft,'autonomy_audit',CAST(:payload AS JSONB)) "
                "ON CONFLICT (idempotency_key) DO NOTHING"
            ),
            {"key": key, "draft": draft_id, "payload": json.dumps(payload)},
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
