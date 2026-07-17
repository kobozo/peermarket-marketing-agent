"""Immutable values shared by autonomous lifecycle policy and execution."""

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit


class _ImmutableMapping(Mapping[str, Any]):
    """Composition-based immutable mapping; it is intentionally not a dict subclass."""

    __slots__ = ("__values",)

    def __init__(self, values: Mapping[str, Any]) -> None:
        if hasattr(self, "_ImmutableMapping__values"):
            raise TypeError("frozen evidence cannot be reinitialized")
        object.__setattr__(self, "_ImmutableMapping__values", dict(values))

    def __getitem__(self, key: str) -> Any:
        return self.__values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.__values)

    def __len__(self) -> int:
        return len(self.__values)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Mapping) and dict(self.items()) == dict(other.items())

    def __setattr__(self, name: str, value: object) -> None:
        raise TypeError("frozen evidence cannot be mutated")

    def __setitem__(self, key: str, value: object) -> None:
        raise TypeError("frozen evidence cannot be mutated")


class _ImmutableSequence(Sequence[Any]):
    """Composition-based immutable sequence; it is intentionally not a list subclass."""

    __slots__ = ("__values",)

    def __init__(self, values: Sequence[Any]) -> None:
        if hasattr(self, "_ImmutableSequence__values"):
            raise TypeError("frozen evidence cannot be reinitialized")
        object.__setattr__(self, "_ImmutableSequence__values", tuple(values))

    def __getitem__(self, index: int | slice) -> Any:
        return self.__values[index]

    def __len__(self) -> int:
        return len(self.__values)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Sequence) and tuple(self) == tuple(other)

    def __setattr__(self, name: str, value: object) -> None:
        raise TypeError("frozen evidence cannot be mutated")

    def append(self, value: object) -> None:
        raise TypeError("frozen evidence cannot be mutated")


def _freeze_json(value: Any) -> Any:
    """Copy JSON-like containers into recursively immutable, serializable values."""
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("evidence mapping keys must be strings")
        return _ImmutableMapping({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return _ImmutableSequence(tuple(_freeze_json(item) for item in value))
    if isinstance(value, (set, frozenset)):
        frozen = (_freeze_json(item) for item in value)
        return _ImmutableSequence(tuple(sorted(frozen, key=repr)))
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"evidence values must be JSON-like, got {type(value).__name__}")


def thaw_json(value: Any) -> Any:
    """Project frozen contract values to plain JSON-native dicts and lists."""
    if isinstance(value, Mapping):
        return {str(key): thaw_json(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [thaw_json(item) for item in value]
    return value


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


def _stable_text_id(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or not value.isascii()
        or any(character.isspace() for character in value)
    ):
        raise ValueError(f"{name} must be a stable non-whitespace ASCII ID")
    return value


def _numeric_meta_id(value: object, name: str) -> str:
    value = _stable_text_id(value, name)
    if not value.isdecimal():
        raise ValueError(f"{name} must be an exact numeric Meta ID")
    return value


def _landing_page(value: object) -> str:
    if not isinstance(value, str) or value != value.strip():
        raise ValueError("landing_page_url must be an exact absolute HTTPS URL")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("landing_page_url must be an exact absolute HTTPS URL")
    return value


@dataclass(frozen=True, slots=True)
class HookVariant:
    """One stable hook variant containing an exact multilingual creative bundle."""

    variant_id: str
    experiment_id: str
    campaign_id: str
    ad_set_id: str
    landing_page_url: str
    changed_dimension: str
    fixed_identity: Mapping[str, Any]
    language_bundles: Mapping[str, Mapping[str, Any]]

    def __post_init__(self) -> None:
        _stable_text_id(self.variant_id, "variant_id")
        _stable_text_id(self.experiment_id, "experiment_id")
        _numeric_meta_id(self.campaign_id, "campaign_id")
        _numeric_meta_id(self.ad_set_id, "ad_set_id")
        _landing_page(self.landing_page_url)
        if self.changed_dimension != "hook":
            raise ValueError("changed_dimension must be exactly hook")
        required_identity = {"audience", "optimization", "format", "visual", "delivery"}
        if not isinstance(self.fixed_identity, Mapping) or not required_identity <= set(
            self.fixed_identity
        ):
            raise ValueError("fixed identity requires audience/optimization/format/visual/delivery")
        if not isinstance(self.language_bundles, Mapping) or set(self.language_bundles) != {
            "NL",
            "FR",
            "EN",
        }:
            raise ValueError("language bundles require exact NL/FR/EN completeness")
        if any(
            not isinstance(bundle, Mapping) or not bundle
            for bundle in self.language_bundles.values()
        ):
            raise ValueError("each NL/FR/EN language bundle must be non-empty")
        bundle_fields = {"hook", "body", "headline", "description", "cta_label"}
        if any(
            set(bundle) != bundle_fields
            or any(not isinstance(value, str) or not value.strip() for value in bundle.values())
            for bundle in self.language_bundles.values()
        ):
            raise ValueError("language bundles require exact complete creative fields")
        object.__setattr__(self, "fixed_identity", _freeze_json(self.fixed_identity))
        object.__setattr__(self, "language_bundles", _freeze_json(self.language_bundles))


@dataclass(frozen=True, slots=True)
class HookExperiment:
    """Exactly three hook variants sharing one frozen delivery identity."""

    experiment_id: str
    campaign_id: str
    ad_set_id: str
    landing_page_url: str
    changed_dimension: str
    fixed_identity: Mapping[str, Any]
    variants: tuple[HookVariant, ...]

    def __post_init__(self) -> None:
        _stable_text_id(self.experiment_id, "experiment_id")
        _numeric_meta_id(self.campaign_id, "campaign_id")
        _numeric_meta_id(self.ad_set_id, "ad_set_id")
        _landing_page(self.landing_page_url)
        if self.changed_dimension != "hook":
            raise ValueError("changed_dimension must be exactly hook")
        if not isinstance(self.fixed_identity, Mapping) or not self.fixed_identity:
            raise ValueError("fixed identity must be a non-empty mapping")
        variants = tuple(self.variants)
        if len(variants) != 3 or any(not isinstance(item, HookVariant) for item in variants):
            raise ValueError("hook experiment requires exactly three variants")
        if len({item.variant_id for item in variants}) != 3:
            raise ValueError("hook experiment variant IDs must be unique")
        identity = _freeze_json(self.fixed_identity)
        if any(
            item.experiment_id != self.experiment_id
            or item.campaign_id != self.campaign_id
            or item.ad_set_id != self.ad_set_id
            or item.landing_page_url != self.landing_page_url
            or item.changed_dimension != self.changed_dimension
            or item.fixed_identity != identity
            for item in variants
        ):
            raise ValueError("variant fixed identity must match its hook experiment")
        non_hook_fields = ("body", "headline", "description", "cta_label")
        baseline = variants[0]
        if any(
            any(
                item.language_bundles[locale][field] != baseline.language_bundles[locale][field]
                for locale in ("NL", "FR", "EN")
                for field in non_hook_fields
            )
            for item in variants[1:]
        ):
            raise ValueError("only hook text may differ across hook variants")
        hook_signatures = {
            tuple(item.language_bundles[locale]["hook"] for locale in ("NL", "FR", "EN"))
            for item in variants
        }
        if len(hook_signatures) != 3:
            raise ValueError("hook experiment requires three distinct hook values")
        object.__setattr__(self, "fixed_identity", identity)
        object.__setattr__(self, "variants", variants)


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
    allocations: Mapping[str, Any] | None = None

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
        if self.kind is DecisionKind.REALLOCATE:
            if not isinstance(self.allocations, Mapping) or set(self.allocations) != {
                "winner",
                "loser",
            }:
                raise ValueError("reallocation requires exact winner and loser allocations")
            frozen = _freeze_json(self.allocations)
            for item in frozen.values():
                if (
                    not isinstance(item, Mapping)
                    or set(item)
                    != {
                        "campaign_id",
                        "variant_id",
                        "ad_set_id",
                        "ad_id",
                        "old_budget_cents",
                        "new_budget_cents",
                    }
                    or not all(
                        isinstance(item[key], str) and item[key].isascii() and item[key].isdecimal()
                        for key in ("ad_set_id", "ad_id")
                    )
                    or not all(
                        type(item[key]) is int and item[key] > 0
                        for key in ("old_budget_cents", "new_budget_cents")
                    )
                ):
                    raise ValueError("reallocation allocations are invalid")
                if (
                    item["campaign_id"] != self.campaign_id
                    or not isinstance(item["variant_id"], str)
                    or not item["variant_id"].strip()
                ):
                    raise ValueError("reallocation allocation ownership is invalid")
            if frozen["winner"]["variant_id"] == frozen["loser"]["variant_id"]:
                raise ValueError("reallocation variants must differ")
            if (
                sum(item["old_budget_cents"] for item in frozen.values()) != self.old_budget_cents
                or sum(item["new_budget_cents"] for item in frozen.values())
                != self.new_budget_cents
                or frozen["winner"]["new_budget_cents"] <= frozen["winner"]["old_budget_cents"]
                or frozen["loser"]["new_budget_cents"] >= frozen["loser"]["old_budget_cents"]
            ):
                raise ValueError("reallocation allocations do not preserve total movement")
            object.__setattr__(self, "allocations", frozen)
        if self.kind is DecisionKind.SCALE and self.allocations is not None:
            if not isinstance(self.allocations, Mapping) or not self.allocations:
                raise ValueError("scale requires every frozen campaign allocation")
            frozen = _freeze_json(self.allocations)
            required = {
                "publication_id",
                "variant_id",
                "campaign_id",
                "ad_set_id",
                "ad_id",
                "old_budget_cents",
                "new_budget_cents",
            }
            if any(
                not isinstance(item, Mapping)
                or set(item) != required
                or item["campaign_id"] != self.campaign_id
                or not all(
                    type(item[key]) is int and item[key] > 0
                    for key in ("publication_id", "old_budget_cents", "new_budget_cents")
                )
                or not all(
                    isinstance(item[key], str) and item[key].isascii() and item[key].isdecimal()
                    for key in ("variant_id", "ad_set_id", "ad_id")
                )
                for item in frozen.values()
            ):
                raise ValueError("scale campaign allocations are invalid")
            if (
                sum(item["old_budget_cents"] for item in frozen.values()) != self.old_budget_cents
                or sum(item["new_budget_cents"] for item in frozen.values())
                != self.new_budget_cents
                or len({item["publication_id"] for item in frozen.values()}) != len(frozen)
                or len({item["ad_set_id"] for item in frozen.values()}) != len(frozen)
            ):
                raise ValueError("scale allocations must match campaign totals and identities")
            object.__setattr__(self, "allocations", frozen)
