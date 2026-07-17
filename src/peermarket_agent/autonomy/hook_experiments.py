"""Pure deterministic construction and validation of multilingual hook experiments."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from peermarket_agent.autonomy.contracts import (
    HookExperiment,
    HookVariant,
    thaw_json,
)
from peermarket_agent.autonomy.replacements import LOCALES, validate_native_hook_bundle

_HOOK_CONCEPTS = (
    {
        "NL": "Verkoop je spullen veilig aan echte kopers",
        "FR": "Vendez vos objets en sécurité à de vrais acheteurs",
        "EN": "Sell your items safely to verified buyers",
    },
    {
        "NL": "Jouw spullen verdienen een betrouwbare koper",
        "FR": "Vos objets méritent un acheteur de confiance",
        "EN": "Your items deserve a buyer you can trust",
    },
    {
        "NL": "Maak ruimte en verkoop veilig aan echte mensen",
        "FR": "Faites de la place et vendez en toute sécurité",
        "EN": "Make space and sell safely to real people",
    },
    {
        "NL": "Geen gedoe, vind veilige kopers voor je spullen",
        "FR": "Évitez les arnaques, trouvez des acheteurs fiables",
        "EN": "Skip the scams and find trustworthy buyers",
    },
    {
        "NL": "Weet aan wie je verkoopt, nog vóór het bericht",
        "FR": "Sachez à qui vous vendez avant le premier message",
        "EN": "Know who you sell to before the first message",
    },
)
HOOK_CATALOG_VERSION = "hook-catalog-v1"
_NON_HOOK_FIELDS = ("body", "headline", "description", "cta_label")


def _read(draft: Mapping[str, Any] | object, name: str) -> Any:
    return draft.get(name) if isinstance(draft, Mapping) else getattr(draft, name)


def _stable_digest(draft: Mapping[str, Any] | object, brand_voice: str, seed: Any) -> str:
    if not isinstance(brand_voice, str) or not brand_voice.strip():
        raise ValueError("brand_voice must be non-empty")
    baseline = _read(draft, "language_bundles")
    if not isinstance(baseline, Mapping) or set(baseline) != set(LOCALES):
        raise ValueError("draft requires exact NL/FR/EN baseline bundles")
    canonical_baseline = {
        locale: {field: baseline[locale][field] for field in _NON_HOOK_FIELDS} for locale in LOCALES
    }
    catalog_digest = hashlib.sha256(
        json.dumps(_HOOK_CONCEPTS, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    stable = {
        "draft_id": _read(draft, "id"),
        "campaign_id": _read(draft, "campaign_id"),
        "ad_set_id": _read(draft, "ad_set_id"),
        "landing_page_url": _read(draft, "landing_page_url"),
        "fixed_identity": thaw_json(_read(draft, "fixed_identity")),
        "baseline_non_hook_bundles": canonical_baseline,
        "seed": {"type": type(seed).__name__, "value": str(seed)},
        "brand_voice_sha256": hashlib.sha256(brand_voice.encode()).hexdigest(),
        "hook_catalog_version": HOOK_CATALOG_VERSION,
        "hook_catalog_sha256": catalog_digest,
    }
    return hashlib.sha256(
        json.dumps(stable, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def build_hook_experiment(
    draft: Mapping[str, Any] | object, brand_voice: str, seed: Any
) -> HookExperiment:
    """Build exactly three stable native hook variants from frozen draft controls."""
    digest = _stable_digest(draft, brand_voice, seed)
    draft_id = _read(draft, "id")
    if type(draft_id) is not int or draft_id <= 0:
        raise ValueError("draft id must be a positive integer")
    experiment_id = f"draft-{draft_id}-hook-{digest[:16]}"
    baseline = _read(draft, "language_bundles")
    if not isinstance(baseline, Mapping) or set(baseline) != set(LOCALES):
        raise ValueError("draft requires exact NL/FR/EN baseline bundles")
    ordered = sorted(
        _HOOK_CONCEPTS,
        key=lambda concept: hashlib.sha256(f"{digest}:{concept['EN']}".encode()).hexdigest(),
    )[:3]
    common = {
        "experiment_id": experiment_id,
        "campaign_id": str(_read(draft, "campaign_id")),
        "ad_set_id": str(_read(draft, "ad_set_id")),
        "landing_page_url": str(_read(draft, "landing_page_url")),
        "changed_dimension": "hook",
        "fixed_identity": _read(draft, "fixed_identity"),
    }
    variants = []
    for number, hooks in enumerate(ordered, start=1):
        validate_native_hook_bundle(hooks)
        variants.append(
            HookVariant(
                variant_id=f"{experiment_id}:{number:02d}",
                language_bundles={
                    locale: {**dict(baseline[locale]), "hook": hooks[locale]} for locale in LOCALES
                },
                **common,
            )
        )
    experiment = HookExperiment(variants=tuple(variants), **common)
    validate_hook_experiment(experiment, draft, brand_voice, seed)
    return experiment


def validate_hook_experiment(
    experiment: HookExperiment,
    draft: Mapping[str, Any] | object,
    brand_voice: str,
    seed: Any,
) -> None:
    """Validate deterministic order, native language hooks, and frozen delivery identity."""
    if thaw_json(experiment.fixed_identity) != thaw_json(_read(draft, "fixed_identity")):
        raise ValueError("hook experiment fixed identity does not match expected controls")
    digest = _stable_digest(draft, brand_voice, seed)
    expected_experiment_id = f"draft-{_read(draft, 'id')}-hook-{digest[:16]}"
    if experiment.experiment_id != expected_experiment_id:
        raise ValueError("hook experiment ID derivation does not match its input commitment")
    expected_ids = tuple(f"{experiment.experiment_id}:{number:02d}" for number in range(1, 4))
    if tuple(item.variant_id for item in experiment.variants) != expected_ids:
        raise ValueError("hook experiment variant ordering or stable IDs changed")
    for variant in experiment.variants:
        validate_native_hook_bundle(
            {locale: variant.language_bundles[locale]["hook"] for locale in LOCALES}
        )
    for locale in LOCALES:
        hooks = [variant.language_bundles[locale]["hook"] for variant in experiment.variants]
        if len(set(hooks)) != 3:
            raise ValueError(f"hook experiment requires three distinct {locale} hooks")
