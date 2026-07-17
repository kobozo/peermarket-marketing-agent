"""Transactional persistence and worker leases for autonomous Meta actions."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from peermarket_agent.autonomy.contracts import ActionStatus, DecisionKind, FrozenDecision


@dataclass(frozen=True, slots=True)
class RecordedDecision:
    id: int
    created: bool


@dataclass(frozen=True, slots=True)
class EnqueuedAction:
    id: int
    decision_id: int
    campaign_id: str
    status: ActionStatus
    created: bool


@dataclass(frozen=True, slots=True)
class ClaimedAction:
    id: int
    decision_id: int
    campaign_id: str
    kind: DecisionKind
    lease_owner: str
    lease_token: str
    lease_expires_at: datetime
    decision: FrozenDecision


@dataclass(frozen=True, slots=True)
class BudgetEvent:
    id: int
    action_id: int
    campaign_id: str
    old_budget_cents: int
    new_budget_cents: int
    amount_cents: int
    created_at: datetime


def _json(value: Any) -> str:
    return json.dumps(value, default=_json_default, sort_keys=True, separators=(",", ":"))


def _json_default(value: Any) -> str:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


async def _record_decision(conn: AsyncConnection, decision: FrozenDecision) -> RecordedDecision:
    inserted = await conn.scalar(
        text(
            "INSERT INTO autonomous_decisions "
            "(decision_key, kind, campaign_id, window_start, window_end, evidence, reason, "
            "old_budget_cents, new_budget_cents) VALUES "
            "(:key, :kind, :campaign_id, :window_start, :window_end, CAST(:evidence AS JSONB), "
            ":reason, :old_budget, :new_budget) ON CONFLICT (decision_key) DO NOTHING RETURNING id"
        ),
        {
            "key": decision.idempotency_key,
            "kind": decision.kind.value,
            "campaign_id": decision.campaign_id,
            "window_start": decision.window_start,
            "window_end": decision.window_end,
            "evidence": _json(decision.evidence),
            "reason": decision.reason,
            "old_budget": decision.old_budget_cents,
            "new_budget": decision.new_budget_cents,
        },
    )
    if inserted is not None:
        return RecordedDecision(int(inserted), True)
    existing = await conn.scalar(
        text("SELECT id FROM autonomous_decisions WHERE decision_key=:key"),
        {"key": decision.idempotency_key},
    )
    return RecordedDecision(int(existing), False)


async def record_decision(engine: AsyncEngine, decision: FrozenDecision) -> RecordedDecision:
    """Append a decision once; a duplicate key always returns the original row."""
    async with engine.begin() as conn:
        return await _record_decision(conn, decision)


async def enqueue_action(engine: AsyncEngine, decision: FrozenDecision) -> EnqueuedAction:
    """Serialize a campaign and enqueue at most one active action."""
    async with engine.begin() as conn:
        await conn.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:campaign_id, 0))"),
            {"campaign_id": decision.campaign_id},
        )
        recorded = await _record_decision(conn, decision)
        existing = (
            (
                await conn.execute(
                    text(
                        "SELECT id, decision_id, campaign_id, status FROM autonomous_actions "
                        "WHERE campaign_id=:campaign_id AND status IN "
                        "('pending','leased','executing','reconciliation_required') "
                        "ORDER BY id DESC LIMIT 1"
                    ),
                    {"campaign_id": decision.campaign_id},
                )
            )
            .mappings()
            .first()
        )
        if existing is not None:
            return _enqueued(existing, False)
        inserted = (
            (
                await conn.execute(
                    text(
                        "INSERT INTO autonomous_actions (decision_id, campaign_id) "
                        "VALUES (:decision_id, :campaign_id) RETURNING id, decision_id, campaign_id, status"
                    ),
                    {"decision_id": recorded.id, "campaign_id": decision.campaign_id},
                )
            )
            .mappings()
            .one()
        )
        return _enqueued(inserted, True)


def _enqueued(row: Any, created: bool) -> EnqueuedAction:
    return EnqueuedAction(
        id=row["id"],
        decision_id=row["decision_id"],
        campaign_id=row["campaign_id"],
        status=ActionStatus(row["status"]),
        created=created,
    )


async def claim_next_action(
    engine: AsyncEngine, worker: str, *, lease_seconds: int = 300
) -> ClaimedAction | None:
    """Lease one pending or abandoned action without waiting on other claimers."""
    if not worker.strip():
        raise ValueError("worker must be non-empty")
    if type(lease_seconds) is not int or lease_seconds <= 0:
        raise ValueError("lease_seconds must be a positive integer")
    token = uuid.uuid4().hex
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE autonomous_actions SET status='reconciliation_required', "
                "failure_category='worker_crash_during_execution', "
                "failure_message='execution lease expired; external state requires reconciliation', "
                "lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL, updated_at=NOW() "
                "WHERE status='executing' AND lease_expires_at <= NOW()"
            )
        )
        row = (
            (
                await conn.execute(
                    text(
                        "WITH candidate AS ("
                        " SELECT id FROM autonomous_actions"
                        " WHERE status='pending' OR (status='leased' AND lease_expires_at <= NOW())"
                        " ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1"
                        ") UPDATE autonomous_actions AS action SET status='leased', lease_owner=:worker,"
                        " lease_token=:token, lease_expires_at=NOW()+make_interval(secs => :seconds),"
                        " updated_at=NOW() FROM candidate, autonomous_decisions AS decision"
                        " WHERE action.id=candidate.id AND decision.id=action.decision_id"
                        " RETURNING action.id, action.decision_id, action.campaign_id, decision.kind,"
                        " action.lease_owner, action.lease_token, action.lease_expires_at,"
                        " decision.decision_key, decision.window_start, decision.window_end,"
                        " decision.evidence, decision.reason, decision.old_budget_cents,"
                        " decision.new_budget_cents"
                    ),
                    {"worker": worker, "token": token, "seconds": lease_seconds},
                )
            )
            .mappings()
            .first()
        )
    if row is None:
        return None
    frozen_decision = FrozenDecision(
        kind=DecisionKind(row["kind"]),
        campaign_id=row["campaign_id"],
        evidence=row["evidence"],
        reason=row["reason"],
        window_start=row["window_start"],
        window_end=row["window_end"],
        idempotency_key=row["decision_key"],
        old_budget_cents=row["old_budget_cents"],
        new_budget_cents=row["new_budget_cents"],
    )
    return ClaimedAction(
        id=row["id"],
        decision_id=row["decision_id"],
        campaign_id=row["campaign_id"],
        kind=DecisionKind(row["kind"]),
        lease_owner=row["lease_owner"],
        lease_token=row["lease_token"],
        lease_expires_at=row["lease_expires_at"],
        decision=frozen_decision,
    )


async def begin_execution(engine: AsyncEngine, claim: ClaimedAction) -> bool:
    """Move a live lease from leased to executing."""
    return await _transition(engine, claim, "leased", "executing")


async def _transition(
    engine: AsyncEngine, claim: ClaimedAction, expected: str, target: str
) -> bool:
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "UPDATE autonomous_actions SET status=:target, updated_at=NOW() "
                "WHERE id=:id AND status=:expected AND lease_owner=:owner AND lease_token=:token "
                "AND lease_expires_at > NOW()"
            ),
            {
                "target": target,
                "id": claim.id,
                "expected": expected,
                "owner": claim.lease_owner,
                "token": claim.lease_token,
            },
        )
        return result.rowcount == 1


async def finish_action(
    engine: AsyncEngine,
    claim: ClaimedAction,
    *,
    status: ActionStatus,
    before_state: Any = None,
    after_state: Any = None,
    rollback_result: Any = None,
    next_evaluation_at: datetime | None = None,
    failure_category: str | None = None,
    failure_message: str | None = None,
    budget: tuple[int, int] | None = None,
) -> bool:
    """Finalize an executing action and atomically append any successful budget event."""
    if status not in {ActionStatus.SUCCEEDED, ActionStatus.FAILED, ActionStatus.CANCELLED}:
        raise ValueError("finish status must be succeeded, failed, or cancelled")
    if budget is not None and status is not ActionStatus.SUCCEEDED:
        raise ValueError("budget events require successful finalization")
    async with engine.begin() as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "UPDATE autonomous_actions SET status=:status, "
                        "before_state=CAST(:before AS JSONB), after_state=CAST(:after AS JSONB), "
                        "audit=CAST(:audit AS JSONB), failure_category=:category, "
                        "failure_message=:message, next_evaluation_at=:next_evaluation, "
                        "lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL, updated_at=NOW() "
                        "WHERE id=:id AND status='executing' AND lease_owner=:owner "
                        "AND lease_token=:token AND lease_expires_at > NOW() "
                        "RETURNING campaign_id, decision_id"
                    ),
                    {
                        "status": status.value,
                        "before": _json(_sanitize_audit_value(before_state or {})),
                        "after": _json(_sanitize_audit_value(after_state or {})),
                        "audit": _json(
                            _sanitize_audit_value(
                                {"rollback_result": rollback_result}
                                if rollback_result is not None
                                else {}
                            )
                        ),
                        "category": _sanitize_category(failure_category),
                        "message": _sanitize_message(failure_message),
                        "next_evaluation": next_evaluation_at,
                        "id": claim.id,
                        "owner": claim.lease_owner,
                        "token": claim.lease_token,
                    },
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return False
        if budget is not None:
            await _insert_budget_event(conn, claim.id, row["campaign_id"], *budget)
        return True


async def release_action(
    engine: AsyncEngine,
    claim: ClaimedAction,
    *,
    failure_category: str | None = None,
    failure_message: str | None = None,
    next_evaluation_at: datetime | None = None,
) -> bool:
    """Return a live leased action to the queue after a retryable failure."""
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "UPDATE autonomous_actions SET status='pending', failure_category=:category, "
                "failure_message=:message, next_evaluation_at=:next_evaluation, lease_owner=NULL, "
                "lease_token=NULL, lease_expires_at=NULL, updated_at=NOW() WHERE id=:id "
                "AND status IN ('leased','executing') AND lease_owner=:owner AND lease_token=:token "
                "AND lease_expires_at > NOW()"
            ),
            {
                "category": _sanitize_category(failure_category),
                "message": _sanitize_message(failure_message),
                "next_evaluation": next_evaluation_at,
                "id": claim.id,
                "owner": claim.lease_owner,
                "token": claim.lease_token,
            },
        )
        return result.rowcount == 1


async def block_campaign_for_reconciliation(
    engine: AsyncEngine,
    claim: ClaimedAction,
    *,
    before_state: Any = None,
    after_state: Any = None,
    rollback_result: Any = None,
    failure_category: str = "reconciliation_required",
    failure_message: str | None = None,
    next_evaluation_at: datetime | None = None,
) -> bool:
    """Terminally block a campaign whose external state is uncertain."""
    async with engine.begin() as conn:
        await conn.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:campaign_id, 0))"),
            {"campaign_id": claim.campaign_id},
        )
        result = await conn.execute(
            text(
                "UPDATE autonomous_actions SET status='reconciliation_required', "
                "before_state=CAST(:before AS JSONB), after_state=CAST(:after AS JSONB), "
                "audit=CAST(:audit AS JSONB), failure_category=:category, failure_message=:message, "
                "next_evaluation_at=:next_evaluation, lease_owner=NULL, lease_token=NULL, "
                "lease_expires_at=NULL, updated_at=NOW() WHERE id=:id AND status='executing' "
                "AND lease_owner=:owner AND lease_token=:token AND lease_expires_at > NOW()"
            ),
            {
                "before": _json(_sanitize_audit_value(before_state or {})),
                "after": _json(_sanitize_audit_value(after_state or {})),
                "audit": _json(
                    _sanitize_audit_value(
                        {"rollback_result": rollback_result} if rollback_result is not None else {}
                    )
                ),
                "category": _sanitize_category(failure_category),
                "message": _sanitize_message(failure_message),
                "next_evaluation": next_evaluation_at,
                "id": claim.id,
                "owner": claim.lease_owner,
                "token": claim.lease_token,
            },
        )
        return result.rowcount == 1


async def record_budget_event(
    engine: AsyncEngine,
    action_id: int,
    campaign_id: str,
    old_budget_cents: int,
    new_budget_cents: int,
) -> BudgetEvent:
    """Append an independently observed budget change."""
    async with engine.begin() as conn:
        return await _insert_budget_event(
            conn, action_id, campaign_id, old_budget_cents, new_budget_cents
        )


async def _insert_budget_event(
    conn: AsyncConnection,
    action_id: int,
    campaign_id: str,
    old_budget_cents: int,
    new_budget_cents: int,
) -> BudgetEvent:
    for value in (old_budget_cents, new_budget_cents):
        if type(value) is not int or value <= 0:
            raise ValueError("budget cents must be positive integers")
    authoritative_campaign_id = await conn.scalar(
        text("SELECT campaign_id FROM autonomous_actions WHERE id=:id FOR SHARE"),
        {"id": action_id},
    )
    if authoritative_campaign_id is None:
        raise ValueError("budget event action does not exist")
    if authoritative_campaign_id != campaign_id:
        raise ValueError("budget event campaign does not match its action")
    row = (
        (
            await conn.execute(
                text(
                    "INSERT INTO autonomous_budget_events "
                    "(action_id, campaign_id, old_budget_cents, new_budget_cents, amount_cents) "
                    "VALUES (:action_id, :campaign_id, :old, :new, :amount) RETURNING *"
                ),
                {
                    "action_id": action_id,
                    "campaign_id": campaign_id,
                    "old": old_budget_cents,
                    "new": new_budget_cents,
                    "amount": new_budget_cents - old_budget_cents,
                },
            )
        )
        .mappings()
        .one()
    )
    return BudgetEvent(**row)


async def campaign_history(engine: AsyncEngine, campaign_id: str) -> list[dict[str, Any]]:
    """Return action audit history with frozen decisions and ordered budget events."""
    async with engine.connect() as conn:
        actions = (
            (
                await conn.execute(
                    text(
                        "SELECT action.*, decision.decision_key, decision.kind, decision.window_start, "
                        "decision.window_end, decision.evidence, decision.reason, "
                        "decision.old_budget_cents, decision.new_budget_cents "
                        "FROM autonomous_actions AS action JOIN autonomous_decisions AS decision "
                        "ON decision.id=action.decision_id WHERE action.campaign_id=:campaign_id "
                        "ORDER BY action.created_at, action.id"
                    ),
                    {"campaign_id": campaign_id},
                )
            )
            .mappings()
            .all()
        )
        events = (
            (
                await conn.execute(
                    text(
                        "SELECT * FROM autonomous_budget_events WHERE campaign_id=:campaign_id "
                        "ORDER BY created_at, id"
                    ),
                    {"campaign_id": campaign_id},
                )
            )
            .mappings()
            .all()
        )
    by_action: dict[int, list[dict[str, Any]]] = {}
    for event in events:
        by_action.setdefault(event["action_id"], []).append(dict(event))
    return [dict(row) | {"budget_events": by_action.get(row["id"], [])} for row in actions]


def _sanitize_category(value: str | None) -> str | None:
    if value is None:
        return None
    sanitized = re.sub(r"[^a-z0-9]+", "_", (_sanitize_message(value) or "unknown").lower()).strip(
        "_"
    )
    return sanitized[:80] or "unknown"


def _sanitize_message(value: str | None) -> str | None:
    if value is None:
        return None
    value = re.sub(
        r"(?i)\b(?:authorization\s*[:=]\s*)?bearer\s+(?:\"[^\"]*\"|'[^']*'|[^\s,;}&\]]+)",
        "Bearer [redacted]",
        value,
    )
    value = re.sub(
        r"(?i)(?:\"|')?(access[_-]?token|token|appsecret_proof|secret|password)(?:\"|')?"
        r"\s*[=:]\s*(?:\"[^\"]*\"|'[^']*'|[^\s&,;}]+)",
        r"\1=[redacted]",
        value,
    )
    return " ".join(value.split())[:500]


def _sanitize_audit_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                "[redacted]"
                if re.fullmatch(
                    r"(?i)authorization|access[_-]?token|token|appsecret_proof|secret|password",
                    str(key),
                )
                else _sanitize_audit_value(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize_audit_value(item) for item in value]
    if isinstance(value, str):
        return _sanitize_message(value)
    return value
