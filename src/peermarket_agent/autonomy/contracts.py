"""Immutable values shared by autonomous lifecycle policy and execution."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


def _immutable(*args: object, **kwargs: object) -> None:
    raise TypeError("frozen evidence cannot be mutated")


class _FrozenDict(dict[str, Any]):
    """A JSON-serializable dict whose mutation API is disabled."""

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable


class _FrozenList(list[Any]):
    """A JSON-serializable list whose mutation API is disabled."""

    __setitem__ = _immutable
    __delitem__ = _immutable
    append = _immutable
    clear = _immutable
    extend = _immutable
    insert = _immutable
    pop = _immutable
    remove = _immutable
    reverse = _immutable
    sort = _immutable
    __iadd__ = _immutable
    __imul__ = _immutable


def _freeze_json(value: Any) -> Any:
    """Copy JSON-like containers into recursively immutable, serializable values."""
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("evidence mapping keys must be strings")
        return _FrozenDict((key, _freeze_json(item)) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return _FrozenList(_freeze_json(item) for item in value)
    if isinstance(value, (set, frozenset)):
        frozen = (_freeze_json(item) for item in value)
        return _FrozenList(sorted(frozen, key=repr))
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"evidence values must be JSON-like, got {type(value).__name__}")


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
            or self.window_start.utcoffset() is None
            or self.window_end.utcoffset() is None
        ):
            raise ValueError("decision window timestamps must be timezone-aware")
        if self.window_start >= self.window_end:
            raise ValueError("window_start must precede window_end")
        if not self.evidence:
            raise ValueError("evidence must be non-empty")
        object.__setattr__(self, "evidence", _freeze_json(self.evidence))
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
