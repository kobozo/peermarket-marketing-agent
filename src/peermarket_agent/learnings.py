"""Evidence gates for reusable marketing learnings."""

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class EvidenceThresholds:
    impressions: int = 1_000
    landing_page_views: int = 30
    registrations: int = 10


DEFAULT_THRESHOLDS = EvidenceThresholds()


@dataclass(frozen=True)
class EvidenceVariant:
    evidence_id: str
    channel: str
    objective: str
    language: str
    audience: str
    window_definition: str
    window_start: date
    window_stop: date
    impressions: int
    landing_page_views: int
    registrations: int | None


@dataclass(frozen=True)
class LearningDecision:
    eligible: bool
    reason: str
    evidence_ids: tuple[str, ...] = ()
    sample: dict[str, int] | None = None


def eligible_learning(
    comparisons: list[EvidenceVariant] | tuple[EvidenceVariant, ...],
    thresholds: EvidenceThresholds,
) -> LearningDecision:
    """Require distinct, exactly comparable variants and evidence from each."""
    evidence_ids = tuple(dict.fromkeys(variant.evidence_id for variant in comparisons))
    if len(evidence_ids) < 2:
        return LearningDecision(False, "requires_comparable_variants")

    dimensions = {
        (
            variant.channel,
            variant.objective,
            variant.language,
            variant.audience,
            variant.window_definition,
            variant.window_start,
            variant.window_stop,
        )
        for variant in comparisons
    }
    if len(dimensions) != 1:
        return LearningDecision(False, "not_comparable")
    if any(
        variant.impressions < thresholds.impressions
        or variant.landing_page_views < thresholds.landing_page_views
        for variant in comparisons
    ):
        return LearningDecision(False, "insufficient_delivery_evidence")
    if any(
        variant.registrations is None or variant.registrations < thresholds.registrations
        for variant in comparisons
    ):
        return LearningDecision(False, "insufficient_conversion_evidence")
    return LearningDecision(
        True,
        "thresholds_met",
        evidence_ids=evidence_ids,
        sample={
            "variants": len(comparisons),
            "impressions": sum(variant.impressions for variant in comparisons),
            "landing_page_views": sum(variant.landing_page_views for variant in comparisons),
            "registrations": sum(variant.registrations or 0 for variant in comparisons),
        },
    )
