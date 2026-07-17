"""Immutable values shared by autonomous lifecycle policy and execution."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class DecisionKind(StrEnum):
    OBSERVE = "observe"
    PAUSE = "pause"
    REPLACE = "replace"
    REALLOCATE = "reallocate"
    SCALE = "scale"


class ActionStatus(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RECONCILIATION_REQUIRED = "reconciliation_required"


@dataclass(frozen=True, slots=True)
class FrozenDecision:
    kind: DecisionKind
    campaign_id: str
    evidence: Mapping[str, Any]
    reason: str
    window_start: datetime | None = None
    window_end: datetime | None = None
    idempotency_key: str = ""
    old_budget_cents: int | None = None
    new_budget_cents: int | None = None

    def __post_init__(self) -> None:
        if not self.campaign_id.isascii() or not self.campaign_id.isdecimal():
            raise ValueError("campaign_id must be an exact numeric Meta campaign ID")
        if (
            self.window_start is None
            or self.window_end is None
            or self.window_start.tzinfo is None
            or self.window_end.tzinfo is None
        ):
            raise ValueError("decision window timestamps must be timezone-aware")
        if self.window_start >= self.window_end:
            raise ValueError("window_start must precede window_end")
        if not self.evidence:
            raise ValueError("evidence must be non-empty")
        if not self.reason.strip():
            raise ValueError("reason must be non-empty")
        if not self.idempotency_key.strip():
            raise ValueError("idempotency_key must be non-empty")
        if self.kind in {DecisionKind.REALLOCATE, DecisionKind.SCALE} and (
            not isinstance(self.old_budget_cents, int)
            or isinstance(self.old_budget_cents, bool)
            or self.old_budget_cents <= 0
            or not isinstance(self.new_budget_cents, int)
            or isinstance(self.new_budget_cents, bool)
            or self.new_budget_cents <= 0
        ):
            raise ValueError("budget actions require positive old and new budget cents")
